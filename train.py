import argparse
import math
import random
import os
import pprint

import numpy as np
import torch
from torch import nn, autograd, optim
from torch.nn import functional as F
from torch.utils import data
import torch.distributed as dist
from torchvision import transforms, utils
from tqdm import tqdm
from transformers import GPT2Model
from torch.autograd import Variable

from miscc.config import cfg, cfg_from_file
from miscc.utils import collapse_dirs, mv_to_paths
from miscc.metrics import compute_ppl
from captions_datasets import TextDataset, ImageFolderDataset, prepare_data
from miscc.losses import words_loss, sent_loss
from miscc.losses import discriminator_loss, generator_loss, KL_loss
from model import RNN_ENCODER , CNN_ENCODER 

try:
    import wandb

except ImportError:
    wandb = None


from dataset import MultiResolutionDataset
from distributed import (
    get_rank,
    synchronize,
    reduce_loss_dict,
    reduce_sum,
    get_world_size,
)
from op import conv2d_gradfix
from non_leaking import augment, AdaptiveAugment

device = "cuda"
device_id = 1
TRANSFORMER_ENCODER = "gpt2"


def data_sampler(dataset, shuffle, distributed):
    if distributed:
        return data.distributed.DistributedSampler(dataset, shuffle=shuffle)

    if shuffle:
        return data.RandomSampler(dataset)

    else:
        return data.SequentialSampler(dataset)


def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        par1[k].data.mul_(decay).add_(par2[k].data, alpha=1 - decay)


def sample_data(loader):
    while True:
        for batch in loader:
            yield batch


def d_logistic_loss(real_pred, fake_pred):
    real_loss = F.softplus(-real_pred)
    fake_loss = F.softplus(fake_pred)

    return real_loss.mean() + fake_loss.mean()


def d_r1_loss(real_pred, real_img):
    with conv2d_gradfix.no_weight_gradients():
        (grad_real,) = autograd.grad(
            outputs=real_pred.sum(), inputs=real_img, create_graph=True
        )
    grad_penalty = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()

    return grad_penalty


def g_nonsaturating_loss(fake_pred):
    loss = F.softplus(-fake_pred).mean()

    return loss


def g_path_regularize(fake_img, latents, mean_path_length, decay=0.01):
    noise = torch.randn_like(fake_img) / math.sqrt(
        fake_img.shape[2] * fake_img.shape[3]
    )
    (grad,) = autograd.grad(
        outputs=(fake_img * noise).sum(), inputs=latents, create_graph=True
    )
    path_lengths = torch.sqrt(grad.pow(2).sum(2).mean(1))

    path_mean = mean_path_length + decay * (path_lengths.mean() - mean_path_length)

    path_penalty = (path_lengths - path_mean).pow(2).mean()

    return path_penalty, path_mean.detach(), path_lengths


def make_noise(batch, latent_dim, n_noise, device):
    if n_noise == 1:
        return torch.randn(batch, latent_dim, device=device)

    noises = torch.randn(n_noise, batch, latent_dim, device=device).unbind(0)

    return noises


def mixing_noise(batch, latent_dim, prob, device):
    if prob > 0 and random.random() < prob:
        return make_noise(batch, latent_dim, 2, device)

    else:
        return [make_noise(batch, latent_dim, 1, device)]


def set_grad_none(model, targets):
    for n, p in model.named_parameters():
        if n in targets:
            p.grad = None


def train(
    args,
    loader,
    generator,
    discriminator,
    text_encoder,
    image_encoder,
    g_optim,
    d_optim,
    g_ema,
    device,
    text_encoder_type="rnn",
    conditioned=False
):
    loader = sample_data(loader)

    pbar = range(args.iter)

    if get_rank() == 0:
        pbar = tqdm(pbar, initial=args.start_iter, dynamic_ncols=True, smoothing=0.01)

    mean_path_length = 0

    d_loss_val = 0
    r1_loss = torch.tensor(0.0, device=device)
    g_loss_val = 0
    path_loss = torch.tensor(0.0, device=device)
    path_lengths = torch.tensor(0.0, device=device)
    mean_path_length_avg = 0
    loss_dict = {}

    if args.distributed:
        g_module = generator.module
        d_module = discriminator.module

    else:
        g_module = generator
        d_module = discriminator

    accum = 0.5 ** (32 / (10 * 1000))
    ada_aug_p = args.augment_p if args.augment_p > 0 else 0.0
    r_t_stat = 0

    if args.augment and args.augment_p == 0:
        ada_augment = AdaptiveAugment(args.ada_target, args.ada_length, 8, device)
    data_batch = next(loader)
    _, sample_captions, cap_lens, class_ids, keys = prepare_data(data_batch)
    sample_captions = sample_captions.to(device)

    sample_z = torch.randn(cfg.TRAIN.BATCH_SIZE, args.latent, device=device)
    if conditioned:
        if text_encoder_type == "rnn":
            hidden = text_encoder.init_hidden(cfg.TRAIN.BATCH_SIZE)
            # print(hidden[0].device)
            sample_words_embs, sample_sent_emb = text_encoder(
                sample_captions, cap_lens, hidden
            )
        elif text_encoder_type == "transformer":
            sample_words_embs = (
                text_encoder(sample_captions)[0].transpose(1, 2).contiguous()
            )
            sample_sent_emb = sample_words_embs[:, :, -1].contiguous()
        # words_embs: batch_size x nef x seq_len
        # sent_emb: batch_size x nef
        sample_words_embs, sample_sent_emb = (
            sample_words_embs.detach(),
            sample_sent_emb.detach(),
        )
        mask = sample_captions == 0
        num_words = sample_words_embs.size(2)
        if mask.size(1) > num_words:
            mask = mask[:, :num_words]

    for idx in pbar:
        i = idx + args.start_iter

        if i > args.iter:
            print("Done!")

            break

        data_batch = next(loader)
        real_img, captions, cap_lens, class_ids, keys = prepare_data(data_batch)
        # print(captions.device)
        # print(cap_lens.device)
        real_img = real_img.to(device)
        captions = captions.to(device)
        

        if conditioned:
            if text_encoder_type == "rnn":
                hidden = text_encoder.init_hidden(real_img.shape[0])
                # print(hidden[0].device)
                words_embs, sent_emb = text_encoder(captions, cap_lens, hidden)
            elif text_encoder_type == "transformer":
                words_embs = text_encoder(captions)[0].transpose(1, 2).contiguous()
                sent_emb = words_embs[:, :, -1].contiguous()
            # words_embs: batch_size x nef x seq_len
            # sent_emb: batch_size x nef
            words_embs, sent_emb = words_embs.detach(), sent_emb.detach()
            mask = captions == 0
            num_words = words_embs.size(2)
            if mask.size(1) > num_words:
                mask = mask[:, :num_words]

        requires_grad(generator, False)
        requires_grad(discriminator, True)

        noise = mixing_noise(real_img.shape[0], args.latent, args.mixing, device)
        if (type(noise) is tuple or type(noise) is list) and conditioned:
            noise = [torch.torch.cat((ns, sent_emb), 1) for ns in noise]
        elif conditioned:
            noise = torch.cat((noise, sent_emb), 1)
    
        fake_img, _ = generator(noise)

        if args.augment:
            real_img_aug, _ = augment(real_img, ada_aug_p)
            fake_img, _ = augment(fake_img, ada_aug_p)

        else:
            real_img_aug = real_img

        real_labels = Variable(torch.FloatTensor(real_img.shape[0], 1).fill_(1)).to(
            device
        )
        fake_labels = Variable(torch.FloatTensor(real_img.shape[0], 1).fill_(0)).to(
            device
        )
        match_labels = Variable(torch.LongTensor(range(cfg.TRAIN.BATCH_SIZE))).to(device)


        fake_pred, fake_logits = discriminator(fake_img, sent_emb)
        real_pred, real_logits = discriminator(real_img_aug, sent_emb)
        d_loss = (
            d_logistic_loss(real_pred, fake_pred)
            + ( nn.BCELoss()(fake_logits, fake_labels)
                + nn.BCELoss()(real_logits, real_labels) ) if conditioned else 0
        )

        loss_dict["d"] = d_loss
        loss_dict["real_score"] = real_pred.mean()
        loss_dict["fake_score"] = fake_pred.mean()

        discriminator.zero_grad()
        d_loss.backward()
        d_optim.step()

        if args.augment and args.augment_p == 0:
            ada_aug_p = ada_augment.tune(real_pred)
            r_t_stat = ada_augment.r_t_stat

        d_regularize = i % args.d_reg_every == 0

        if d_regularize:
            real_img.requires_grad = True

            if args.augment:
                real_img_aug, _ = augment(real_img, ada_aug_p)

            else:
                real_img_aug = real_img

            real_pred, _ = discriminator(real_img_aug, sent_emb)
            r1_loss = d_r1_loss(real_pred, real_img)

            discriminator.zero_grad()
            (args.r1 / 2 * r1_loss * args.d_reg_every + 0 * real_pred[0]).backward()

            d_optim.step()

        loss_dict["r1"] = r1_loss

        requires_grad(generator, True)
        requires_grad(discriminator, False)

        noise = mixing_noise(real_img.shape[0], args.latent, args.mixing, device)
        if (type(noise) is tuple or type(noise) is list) and conditioned:
            noise = [torch.torch.cat((ns, sent_emb), 1) for ns in noise]
        elif conditioned:
            noise = torch.cat((noise, sent_emb), 1)

        fake_img, _ = generator(noise)

        if args.augment:
            fake_img, _ = augment(fake_img, ada_aug_p)

        fake_pred, _ = discriminator(fake_img, sent_emb)

        g_loss = g_nonsaturating_loss(fake_pred)

        ## add img encoder and sent lostt
        region_features, cnn_code = image_encoder(fake_img)


        s_loss0, s_loss1 = sent_loss(cnn_code, sent_emb,
                                         match_labels, class_ids, cfg.TRAIN.BATCH_SIZE)

        s_loss = (s_loss0 + s_loss1) * \
                cfg.TRAIN.SMOOTH.LAMBDA

        ### add s_loss to g_loss
        g_loss+=s_loss   
            
        loss_dict["g"] = g_loss
        loss_dict['sent']=s_loss

        generator.zero_grad()
        g_loss.backward()
        g_optim.step()

        g_regularize = i % args.g_reg_every == 0
        # It doesn't make sense to do a PPL for text embeddings since this completely changes the image features
        # if g_regularize:
        #     path_batch_size = max(1, real_img.shape[0] // args.path_batch_shrink)
        #     noise = mixing_noise(path_batch_size, args.latent, args.mixing, device)
        #     if type(noise) is tuple or type(noise) is list:
        #         noise = [
        #             torch.torch.cat((ns, sent_emb[:path_batch_size]), 1) for ns in noise
        #         ]
        #     else:
        #         noise = torch.cat((noise, sent_emb), 1)
        #     fake_img, latents = generator(noise, return_latents=True)

        #     path_loss, mean_path_length, path_lengths = g_path_regularize(
        #         fake_img, latents, mean_path_length
        #     )

        #     generator.zero_grad()
        #     weighted_path_loss = args.path_regularize * args.g_reg_every * path_loss

        #     if args.path_batch_shrink:
        #         weighted_path_loss += 0 * fake_img[0, 0, 0, 0]

        #     weighted_path_loss.backward()

        #     g_optim.step()

        #     mean_path_length_avg = (
        #         reduce_sum(mean_path_length).item() / get_world_size()
        #     )

        # loss_dict["path"] = path_loss
        # loss_dict["path_length"] = path_lengths.mean()

        accumulate(g_ema, g_module, accum)

        loss_reduced = reduce_loss_dict(loss_dict)

        d_loss_val = loss_reduced["d"].mean().item()
        g_loss_val = loss_reduced["g"].mean().item()
        r1_val = loss_reduced["r1"].mean().item()
        # path_loss_val = loss_reduced["path"].mean().item()
        real_score_val = loss_reduced["real_score"].mean().item()
        fake_score_val = loss_reduced["fake_score"].mean().item()
        sent_loss_val=loss_reduced['sent'].mean().item()
        # path_length_val = loss_reduced["path_length"].mean().item()

        if get_rank() == 0:
            pbar.set_description(
                (
                    f"d: {d_loss_val:.4f}; g: {g_loss_val:.4f}; r1: {r1_val:.4f};  sen: {sent_loss_val:.4f};"
                    # f"path: {path_loss_val:.4f}; mean path: {mean_path_length_avg:.4f}; "
                )
            )

            if wandb and args.wandb:
                wandb.log(
                    {
                        "Generator": g_loss_val,
                        "Discriminator": d_loss_val,
                        "Augment": ada_aug_p,
                        "Rt": r_t_stat,
                        "R1": r1_val,
                        # "Path Length Regularization": path_loss_val,
                        # "Mean Path Length": mean_path_length,
                        "Real Score": real_score_val,
                        "Fake Score": fake_score_val,
                        # "Path Length": path_length_val,
                    }
                )

            if i % 100 == 0:
                with torch.no_grad():
                    g_ema.eval()
                    sample_encoded = torch.cat((sample_z, sample_sent_emb), 1) \
                                    if conditioned else sample_z

                    sample, _ = g_ema([sample_encoded])
                    utils.save_image(
                        sample,
                        f"sample/{str(i).zfill(6)}.png",
                        nrow=int(args.n_sample ** 0.5),
                        normalize=True,
                        range=(-1, 1),
                    )

            if i % 10000 == 0:
                torch.save(
                    {
                        "g": g_module.state_dict(),
                        "d": d_module.state_dict(),
                        "g_ema": g_ema.state_dict(),
                        "g_optim": g_optim.state_dict(),
                        "d_optim": d_optim.state_dict(),
                        "args": args,
                        "ada_aug_p": ada_aug_p,
                    },
                    f"checkpoint/{str(i).zfill(6)}.pt",
                )


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="StyleGAN2 trainer")

    # parser.add_argument("path", type=str, help="path to the lmdb dataset")
    parser.add_argument(
        "--cfg",
        dest="cfg_file",
        help="optional config file",
        default="cfg/bird_attn2_style.yml",
        type=str,
    )
    parser.add_argument("--text_encoder_type", type=str.casefold, default="rnn")
    parser.add_argument(
        "--arch",
        type=str,
        default="stylegan2",
        help="model architectures (stylegan2 | swagan)",
    )
    parser.add_argument(
        "--iter", type=int, default=800000, help="total training iterations"
    )
    parser.add_argument(
        "--batch", type=int, default=cfg.TRAIN.BATCH_SIZE, help="batch sizes for each gpus"
    )
    parser.add_argument(
        "--n_sample",
        type=int,
        default=64,
        help="number of the samples generated during training",
    )
    parser.add_argument(
        "--size", type=int, default=256, help="image sizes for the model"
    )
    parser.add_argument(
        "--r1", type=float, default=10, help="weight of the r1 regularization"
    )
    parser.add_argument(
        "--path_regularize",
        type=float,
        default=2,
        help="weight of the path length regularization",
    )
    parser.add_argument(
        "--path_batch_shrink",
        type=int,
        default=2,
        help="batch size reducing factor for the path length regularization (reduce memory consumption)",
    )
    parser.add_argument(
        "--d_reg_every",
        type=int,
        default=16,
        help="interval of the applying r1 regularization",
    )
    parser.add_argument(
        "--g_reg_every",
        type=int,
        default=4,
        help="interval of the applying path length regularization",
    )
    parser.add_argument(
        "--mixing", type=float, default=0.9, help="probability of latent code mixing"
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default=None,
        help="path to the checkpoints to resume training",
    )
    parser.add_argument("--lr", type=float, default=0.002, help="learning rate")
    parser.add_argument(
        "--channel_multiplier",
        type=int,
        default=2,
        help="channel multiplier factor for the model. config-f = 2, else = 1",
    )
    parser.add_argument(
        "--wandb", action="store_true", help="use weights and biases logging"
    )
    parser.add_argument(
        "--local_rank", type=int, default=0, help="local rank for distributed training"
    )
    parser.add_argument(
        "--augment", action="store_true", help="apply non leaking augmentation"
    )
    parser.add_argument(
        "--augment_p",
        type=float,
        default=0,
        help="probability of applying augmentation. 0 = use adaptive augmentation",
    )
    parser.add_argument(
        "--ada_target",
        type=float,
        default=0.6,
        help="target augmentation probability for adaptive augmentation",
    )
    parser.add_argument(
        "--ada_length",
        type=int,
        default=500 * 1000,
        help="target duraing to reach augmentation probability for adaptive augmentation",
    )
    parser.add_argument(
        "--ada_every",
        type=int,
        default=256,
        help="probability update interval of the adaptive augmentation",
    )
    parser.add_argument(
        "--enc_size", type=int, default=256, help="size of the sentence embeddings",
    )
    parser.add_argument(
        "--cond", type=bool, default=False, help="size of the sentence embeddings",
    )

    args = parser.parse_args()

    n_gpu = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    args.distributed = n_gpu > 1

    if args.distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        synchronize()

    args.latent = 512
    args.n_mlp = 8

    args.start_iter = 0
    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.arch == "stylegan2":
        from model import Generator, Discriminator

    elif args.arch == "swagan":
        from swagan import Generator, Discriminator
    generator = Generator(
        args.size,
        args.latent + cfg.TEXT.EMBEDDING_DIM if args.cond else 0,
        args.n_mlp,
        channel_multiplier=args.channel_multiplier,
    ).to(device)
    discriminator = Discriminator(
        args.size, channel_multiplier=args.channel_multiplier
    ).to(device)
    g_ema = Generator(
        args.size,
        args.latent + cfg.TEXT.EMBEDDING_DIM if args.cond else 0,
        args.n_mlp,
        channel_multiplier=args.channel_multiplier,
    ).to(device)
    g_ema.eval()
    accumulate(g_ema, generator, 0)

    split_dir, bshuffle = "train", True
    if not cfg.TRAIN.FLAG:
        # bshuffle = False
        split_dir = "test"

    transform = transforms.Compose(
        [
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True),
        ]
    )

    dataset = TextDataset(
        cfg.DATA_DIR,
        args.text_encoder_type,
        split_dir,
        base_size=cfg.TREE.BASE_SIZE,
        transform=transform,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=cfg.TRAIN.BATCH_SIZE,
        drop_last=True,
        shuffle=bshuffle,
        num_workers=int(cfg.WORKERS),
    )

    if args.text_encoder_type == "rnn":
        text_encoder = RNN_ENCODER(dataset.n_words, nhidden=cfg.TEXT.EMBEDDING_DIM)
    elif args.text_encoder_type == "transformer":
        text_encoder = GPT2Model.from_pretrained(TRANSFORMER_ENCODER)

    state_dict = torch.load(cfg.TRAIN.NET_E, map_location=lambda storage, loc: storage)
    text_encoder.load_state_dict(state_dict)
    for p in text_encoder.parameters():
        p.requires_grad = False
    print("Load text encoder from:", cfg.TRAIN.NET_E)
    text_encoder.eval()
    text_encoder = text_encoder.to(device)

    #img encoder
    image_encoder = CNN_ENCODER(cfg.TEXT.EMBEDDING_DIM)
    img_encoder_path = cfg.TRAIN.NET_E.replace('text_encoder', 'image_encoder')
    state_dict = \
        torch.load(img_encoder_path, map_location=lambda storage, loc: storage)
    image_encoder.load_state_dict(state_dict)
    for p in image_encoder.parameters():
        p.requires_grad = False
    print('Load image encoder from:', img_encoder_path)
    image_encoder.eval()
    image_encoder=image_encoder.to(device)



    g_reg_ratio = args.g_reg_every / (args.g_reg_every + 1)
    d_reg_ratio = args.d_reg_every / (args.d_reg_every + 1)

    g_optim = optim.Adam(
        generator.parameters(),
        lr=args.lr * g_reg_ratio,
        betas=(0 ** g_reg_ratio, 0.99 ** g_reg_ratio),
    )
    d_optim = optim.Adam(
        discriminator.parameters(),
        lr=args.lr * d_reg_ratio,
        betas=(0 ** d_reg_ratio, 0.99 ** d_reg_ratio),
    )

    if args.ckpt is not None:
        print("load model:", args.ckpt)

        ckpt = torch.load(args.ckpt, map_location=lambda storage, loc: storage)

        try:
            ckpt_name = os.path.basename(args.ckpt)
            args.start_iter = int(os.path.splitext(ckpt_name)[0])

        except ValueError:
            pass

        generator.load_state_dict(ckpt["g"])
        discriminator.load_state_dict(ckpt["d"])
        g_ema.load_state_dict(ckpt["g_ema"])

        g_optim.load_state_dict(ckpt["g_optim"])
        d_optim.load_state_dict(ckpt["d_optim"])

    if args.distributed:
        generator = nn.parallel.DistributedDataParallel(
            generator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

        discriminator = nn.parallel.DistributedDataParallel(
            discriminator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

        # text_encoder = nn.parallel.DistributedDataParallel(
        #     text_encoder,
        #     device_ids=[args.local_rank],
        #     output_device=args.local_rank,
        #     broadcast_buffers=False,
        # )

        # image_encoder=nn.parallel.DistributedDataParallel(
        #     image_encoder,
        #     device_ids=[args.local_rank],
        #     output_device=args.local_rank,
        #     broadcast_buffers=False,
        # )


    # dataset = MultiResolutionDataset(args.path, transform, args.size)
    # loader = data.DataLoader(
    #     dataset,
    #     batch_size=args.batch,
    #     sampler=data_sampler(dataset, shuffle=True, distributed=args.distributed),
    #     drop_last=True,
    # )

    if get_rank() == 0 and wandb is not None and args.wandb:
        wandb.init(project="stylegan 2")
    train(
        args,
        dataloader,
        generator,
        discriminator,
        text_encoder,
        image_encoder,
        g_optim,
        d_optim,
        g_ema,
        device,
        conditioned=args.cond
    )

