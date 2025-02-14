import math
from omegaconf import OmegaConf

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from diffusers import DDIMScheduler

import sys
from os import path

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

from ldm.util import instantiate_from_config
from guidance.guidance_utils import SpecifyGradient


# load model
def load_model_from_config(config, ckpt, device, vram_O=False, verbose=False):
    pl_sd = torch.load(ckpt, map_location='cpu')

    if 'global_step' in pl_sd and verbose:
        print(f'[INFO] Global Step: {pl_sd["global_step"]}')

    sd = pl_sd['state_dict']

    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)

    if len(m) > 0 and verbose:
        print('[INFO] missing keys: \n', m)
    if len(u) > 0 and verbose:
        print('[INFO] unexpected keys: \n', u)

    # manually load ema and delete it to save GPU memory
    if model.use_ema:
        if verbose:
            print('[INFO] loading EMA...')
        model.model_ema.copy_to(model.model)
        del model.model_ema

    if vram_O:
        # we don't need decoder
        del model.first_stage_model.decoder

    torch.cuda.empty_cache()

    model.eval().to(device)

    return model


class Zero123(nn.Module):
    def __init__(self, device, fp16=False, vram_O=False, t_range=[0.02, 0.98]):
        super().__init__()

        # hardcoded
        config = './pretrained/zero123/sd-objaverse-finetune-c_concat-256.yaml'
        ckpt = './pretrained/zero123/zero123-xl.ckpt'

        self.device = device
        self.fp16 = fp16

        self.config = OmegaConf.load(config)
        # TODO: seems it cannot load into fp16...
        self.model = load_model_from_config(self.config, ckpt, device=self.device, vram_O=vram_O)

        # timesteps: use diffuser for convenience... hope it's alright.
        self.num_train_timesteps = self.config.model.params.timesteps

        self.scheduler = DDIMScheduler(
            self.num_train_timesteps,
            self.config.model.params.linear_start,
            self.config.model.params.linear_end,
            beta_schedule='scaled_linear',
            clip_sample=False,
            set_alpha_to_one=False,
            steps_offset=1,
        )

        self.min_step = int(self.num_train_timesteps * t_range[0])
        self.max_step = int(self.num_train_timesteps * t_range[1])
        self.alphas = self.scheduler.alphas_cumprod.to(self.device)  # for convenience

    @torch.no_grad()
    def get_img_embeds(self, x):
        # x: image tensor [1, 3, 256, 256] in [0, 1]
        x = x * 2 - 1
        c = self.model.get_learned_conditioning(x)  # .tile(n_samples, 1, 1)
        v = self.model.encode_first_stage(x).mode()
        return c, v

    def train_step(self, embeddings, pred_rgb, dT, guidance_scale=100,
                   as_latent=False, grad_scale=1, t_range=[0.02, 0.98], t=None):
        # pred_rgb: tensor [1, 3, H, W] in [0, 1]
        B = pred_rgb.size(0)
        pred_rgb = pred_rgb * 2 - 1

        if as_latent:
            latents = F.interpolate(pred_rgb, (32, 32), mode='bilinear', align_corners=False) * 2 - 1
        else:
            pred_rgb_256 = F.interpolate(pred_rgb, (256, 256), mode='bilinear', align_corners=False)
            latents = self.encode_imgs(pred_rgb_256)

        if t is None:
            min_step = int(self.num_train_timesteps * t_range[0])
            max_step = int(self.num_train_timesteps * t_range[1])
            t = torch.randint(min_step, max_step + 1, [B], dtype=torch.long, device=self.device)
        else:
            t = torch.tensor(t, dtype=torch.long, device=self.device).expand(B)

        with torch.no_grad():
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t)

            x_in = torch.cat([latents_noisy] * 2)
            t_in = torch.cat([t] * 2)
            T = dT
            cond = {}
            # clip_emb = self.model.cc_projection(torch.cat([embeddings[0], T], dim=-1))
            # cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb).to(self.device), clip_emb], dim=0)]
            # cond['c_concat'] = [torch.cat([torch.zeros_like(embeddings[1]).to(self.device), embeddings[1]], dim=0)]
            clip_emb = self.model.cc_projection(
                torch.cat([embeddings[0].expand(B, -1, -1), T[:, None, :]], dim=-1))
            cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb), clip_emb], dim=0)]
            vae_emb = embeddings[1].expand(B, -1, -1, -1)
            cond['c_concat'] = [torch.cat([torch.zeros_like(vae_emb), vae_emb], dim=0)]

            noise_pred = self.model.apply_model(x_in, t_in, cond)

        noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_cond - noise_pred_uncond)

        w = (1 - self.alphas[t])[:, None, None, None]
        grad = grad_scale * w * (noise_pred - noise)
        grad = torch.nan_to_num(grad)

        # since we omitted an item in grad, we need to use the custom function to specify the gradient
        loss = SpecifyGradient.apply(latents, grad)

        return loss

    def train_step_BGT(self, embeddings, pred_rgb, dT, guidance_scale=100,
                       as_latent=False, t=0.98, r=0.25, w_rgb=0.1):

        # pred_rgb: tensor [1, 3, H, W] in [0, 1]
        B = pred_rgb.size(0)
        pred_rgb = pred_rgb * 2 - 1
        pred_rgb_256 = F.interpolate(pred_rgb, (256, 256), mode='bilinear', align_corners=False)
        if as_latent:
            latents = F.interpolate(pred_rgb, (32, 32), mode='bilinear', align_corners=False) * 2 - 1
        else:
            latents = self.encode_imgs(pred_rgb_256)

        with torch.no_grad():

            noise = torch.randn_like(latents)
            timestep = torch.tensor([t*self.num_train_timesteps] * B, dtype=torch.long, device=self.device)
            latents_noisy = self.scheduler.add_noise(latents, noise, timestep)

            T = dT
            cond = {}
            clip_emb = self.model.cc_projection(
                torch.cat([embeddings[0].expand(B, -1, -1), T[:, None, :]], dim=-1))
            cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb), clip_emb], dim=0)]
            vae_emb = embeddings[1].expand(B, -1, -1, -1)
            cond['c_concat'] = [torch.cat([torch.zeros_like(vae_emb), vae_emb], dim=0)]

            num_inference_steps = math.ceil(t/r)
            self.scheduler.num_inference_steps = num_inference_steps
            step_ratio = self.num_train_timesteps * r
            timesteps = (np.arange(0, num_inference_steps) * step_ratio).round()[::-1].copy().astype(np.int64)
            dt = math.ceil(t * self.num_train_timesteps - (num_inference_steps - 1) * step_ratio)
            if dt > 0:
                timesteps += dt
            self.scheduler.timesteps = torch.from_numpy(np.append(timesteps, [0])).to(self.device)
            for i, t in enumerate(self.scheduler.timesteps):
                timestep = t.expand(B)
                x_in = torch.cat([latents_noisy] * 2)
                t_in = torch.cat([timestep] * 2)

                noise_pred = self.model.apply_model(x_in, t_in, cond)

                # perform guidance
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_text + guidance_scale * (noise_pred_text - noise_pred_uncond)

                # compute the previous noisy sample x_t -> x_t-1
                latents_noisy = self.scheduler.step(noise_pred, t, latents_noisy)['prev_sample']

            if w_rgb > 0:
                img_gt = self.decode_latents(latents_noisy)

        loss = torch.square(latents - latents_noisy).mean()
        if w_rgb > 0:
            loss += w_rgb * torch.square(pred_rgb_256 - img_gt).mean()

        return loss

    # verification
    @torch.no_grad()
    def __call__(self,
                 image, T=None, # image tensor [1, 3, H, W] in [0, 1]
                 polar=0, azimuth=0, radius=0,  # new view params
                 scale=3, ddim_steps=50, ddim_eta=1, h=256, w=256,  # diffusion params
                 ):

        embeddings = self.get_img_embeds(image)
        if T is None:
            T = torch.tensor(
                [math.radians(polar), math.sin(math.radians(azimuth)), math.cos(math.radians(azimuth)), radius])
            T = T[None, None, :].to(self.device)

        cond = {}
        clip_emb = self.model.cc_projection(torch.cat([embeddings[0], T], dim=-1))
        cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb).to(self.device), clip_emb], dim=0)]
        cond['c_concat'] = [torch.cat([torch.zeros_like(embeddings[1]).to(self.device), embeddings[1]], dim=0)]

        # produce latents loop
        latents = torch.randn((1, 4, h // 8, w // 8), device=self.device)
        self.scheduler.set_timesteps(ddim_steps)

        for i, t in enumerate(self.scheduler.timesteps):
            x_in = torch.cat([latents] * 2)
            t_in = torch.cat([t.view(1)] * 2).to(self.device)

            noise_pred = self.model.apply_model(x_in, t_in, cond)
            noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + scale * (noise_pred_cond - noise_pred_uncond)

            latents = self.scheduler.step(noise_pred, t, latents, eta=ddim_eta)['prev_sample']

        imgs = self.decode_latents(latents)
        imgs = imgs.cpu().numpy().transpose(0, 2, 3, 1)

        return imgs

    def decode_latents(self, latents):
        # zs: [B, 4, 32, 32] Latent space image
        # with self.model.ema_scope():
        imgs = self.model.decode_first_stage(latents)
        imgs = (imgs / 2 + 0.5).clamp(0, 1)

        return imgs  # [B, 3, 256, 256] RGB space image

    def encode_imgs(self, imgs):
        # imgs: [B, 3, 256, 256] RGB space image
        # with self.model.ema_scope():
        imgs = imgs * 2 - 1
        latents = self.model.get_first_stage_encoding(self.model.encode_first_stage(imgs))
        return latents  # [B, 4, 32, 32] Latent space image


if __name__ == '__main__':
    import cv2
    import argparse
    import numpy as np
    import matplotlib.pyplot as plt

    parser = argparse.ArgumentParser()

    parser.add_argument('input', type=str)
    parser.add_argument('--fp16', action='store_true',
                        help="use float16 for training")  # no use now, can only run in fp32

    parser.add_argument('--polar', type=float, default=0, help='delta polar angle in [-90, 90]')
    parser.add_argument('--azimuth', type=float, default=0, help='delta azimuth angle in [-180, 180]')
    parser.add_argument('--radius', type=float, default=0, help='delta camera radius multiplier in [-0.5, 0.5]')

    opt = parser.parse_args()

    device = torch.device('cuda')

    print(f'[INFO] loading image from {opt.input} ...')
    image = cv2.imread(opt.input, cv2.IMREAD_UNCHANGED)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (256, 256), interpolation=cv2.INTER_AREA)
    image = image.astype(np.float32) / 255.0
    image = torch.from_numpy(image).permute(2, 0, 1).unsqueeze(0).contiguous().to(device)

    print(f'[INFO] loading model ...')
    zero123 = Zero123(device, opt.fp16)

    print(f'[INFO] running model ...')
    outputs = zero123(image, polar=opt.polar, azimuth=opt.azimuth, radius=opt.radius)
    plt.imshow(outputs[0])
    plt.show()
