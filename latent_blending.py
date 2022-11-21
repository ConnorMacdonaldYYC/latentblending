# Copyright 2022 Lunar Ring. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os, sys
dp_git = "/home/lugo/git/"
sys.path.append(os.path.join(dp_git,'garden4'))
sys.path.append('util')
import torch
torch.backends.cudnn.benchmark = False
import numpy as np
import warnings
warnings.filterwarnings('ignore')
import time
import subprocess
import warnings
import torch
from tqdm.auto import tqdm
from diffusers import StableDiffusionInpaintPipeline
from diffusers import StableDiffusionPipeline
from diffusers.schedulers import DDIMScheduler
from PIL import Image
import matplotlib.pyplot as plt
import torch
from movie_man import MovieSaver
import datetime
from typing import Callable, List, Optional, Union
import inspect
torch.set_grad_enabled(False)

#%% 
class LatentBlending():
    def __init__(
            self, 
            pipe: Union[StableDiffusionInpaintPipeline, StableDiffusionPipeline],
            device: str,
            height: int = 512,
            width: int = 512,
            num_inference_steps: int = 30,
            guidance_scale: float = 7.5,
            seed: int = 420,
        ):
        r"""
        Initializes the latent blending class.
        Args:
            device: str
                Compute device, e.g. cuda:0
            height: int
                Height of the desired output image. The model was trained on 512.
            width: int
                Width of the desired output image. The model was trained on 512.
            num_inference_steps: int
                Number of diffusion steps. Larger values will take more compute time.
            guidance_scale: float
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            seed: int
                Random seed.
            
        """
        
        self.pipe = pipe
        self.device = device
        self.guidance_scale = guidance_scale
        self.num_inference_steps = num_inference_steps
        self.width = width
        self.height = height
        self.seed = seed
    
        # Inits 
        self.check_asserts()
        self.init_mode()
        
        # Initialize vars
        self.prompt1 = ""
        self.prompt2 = ""
        self.tree_latents = []
        self.tree_fracts = []
        self.tree_status = []
        self.tree_final_imgs = []
        self.list_nmb_branches_prev = []
        self.list_injection_idx_prev = []
        self.text_embedding1 = None
        self.text_embedding2 = None
        
    
    def check_asserts(self):
        r"""
        Runs Minimal set of sanity checks.
        """
        assert self.pipe.scheduler._class_name == 'DDIMScheduler', 'Currently only the DDIMScheduler is supported.'
        
        
    def init_mode(self):
        r"""
        Automatically sets the mode of this class, depending on the supplied pipeline.
        """
        if self.pipe._class_name == 'StableDiffusionInpaintPipeline':
            self.mask_empty = Image.fromarray(255*np.ones([self.width, self.height], dtype=np.uint8))
            self.image_empty = Image.fromarray(np.zeros([self.width, self.height, 3], dtype=np.uint8))
            self.image_source = None
            self.mask_image = None
            self.mode = 'inpaint'
        else:
            self.mode = 'standard'
            
        
    def init_inpainting(
            self, 
            image_source: Union[Image.Image, np.ndarray] = None, 
            mask_image: Union[Image.Image, np.ndarray] = None, 
            init_empty: Optional[bool] = False,
        ):
        r"""
        Initializes inpainting with a source and maks image.
        Args:
            image_source: Union[Image.Image, np.ndarray]
                Source image onto which the mask will be applied.
            mask_image: Union[Image.Image, np.ndarray]
                Mask image, value = 0 will stay untouched, value = 255 subjet to diffusion
            init_empty: Optional[bool]:
                Initialize inpainting with an empty image and mask, effectively disabling inpainting.
        """
        assert self.mode == 'inpaint', 'Initialize class with an inpainting pipeline!'
        if not init_empty:
            assert image_source is not None, "init_inpainting: you need to provide image_source"
            assert mask_image is not None, "init_inpainting: you need to provide mask_image"
            if type(image_source) == np.ndarray:
                image_source = Image.fromarray(image_source)
            self.image_source = image_source
            
            if type(mask_image) == np.ndarray:
                mask_image = Image.fromarray(mask_image)
            self.mask_image = mask_image
        else:
            self.mask_image  = self.mask_empty
            self.image_source  = self.image_empty


    def set_prompt1(self, prompt: str):
        r"""
        Sets the first prompt (for the first keyframe) including text embeddings.
        Args:
            prompt: str
                ABC trending on artstation painted by Greg Rutkowski
        """
        prompt = prompt.replace("_", " ")
        self.prompt1 = prompt
        self.text_embedding1 = self.get_text_embeddings(self.prompt1)
        
    
    def set_prompt2(self, prompt: str):
        r"""
        Sets the second prompt (for the second keyframe) including text embeddings.
        Args:
            prompt: str
                XYZ trending on artstation painted by Greg Rutkowski
        """
        prompt = prompt.replace("_", " ")
        self.prompt2 = prompt
        self.text_embedding2 = self.get_text_embeddings(self.prompt2)
        
    
    def run_transition(
            self, 
            list_nmb_branches: List[int], 
            list_injection_strength: List[float] = None, 
            list_injection_idx: List[int] = None, 
            recycle_img1: Optional[bool] = False, 
            recycle_img2: Optional[bool] = False, 
            fixed_seeds: Optional[List[int]] = None,
        ):
        r"""
        Returns a list of transition images using spherical latent blending.
        Args:
            list_nmb_branches: List[int]:
                list of the number of branches for each injection.
            list_injection_strength: List[float]:
                list of injection strengths within interval [0, 1), values need to be increasing.
                Alternatively you can direclty specify the list_injection_idx.
            list_injection_idx: List[int]:
                list of injection strengths within interval [0, 1), values need to be increasing.
                Alternatively you can specify the list_injection_strength.
            recycle_img1: Optional[bool]:
                Don't recompute the latents for the first keyframe (purely prompt1). Saves compute.
            recycle_img2: Optional[bool]:
                Don't recompute the latents for the second keyframe (purely prompt2). Saves compute.
            fixed_seeds: Optional[List[int)]:
                You can supply two seeds that are used for the first and second keyframe (prompt1 and prompt2).
                Otherwise random seeds will be taken.
            
        """
        # Sanity checks first
        assert self.text_embedding1 is not None, 'Set the first text embedding with .set_prompt1(...) first'
        assert self.text_embedding2 is not None, 'Set the second text embedding with .set_prompt2(...) first'
        assert not((list_injection_strength is not None) and (list_injection_idx is not None)), "suppyl either list_injection_strength or list_injection_idx"
        
        if list_injection_strength is None:
            assert list_injection_idx is not None, "Supply either list_injection_idx or list_injection_strength"
            assert type(list_injection_idx[0]) is int, "Need to supply integers for list_injection_idx"
            
        if list_injection_idx is None:
            assert list_injection_strength is not None, "Supply either list_injection_idx or list_injection_strength"
            # Create the injection indexes
            list_injection_idx = [int(round(x*self.num_inference_steps)) for x in list_injection_strength]
            assert min(np.diff(list_injection_idx)) > 0, 'Injection idx needs to be increasing'
            if min(np.diff(list_injection_idx)) < 2:
                print("Warning: your injection spacing is very tight. consider increasing the distances")
            assert type(list_injection_strength[1]) is float, "Need to supply floats for list_injection_strength"
            # we are checking element 1 in list_injection_strength because "0" is an int... [0, 0.5]
        
        assert max(list_injection_idx) < self.num_inference_steps, "Decrease the injection index or strength"
        assert len(list_injection_idx) == len(list_nmb_branches), "Need to have same length"
        assert max(list_injection_idx) < self.num_inference_steps,"Injection index cannot happen after last diffusion step! Decrease list_injection_idx or list_injection_strength[-1]"
        
        if fixed_seeds is not None:
            if fixed_seeds == 'randomize':
                fixed_seeds = list(np.random.randint(0, 1000000, 2).astype(np.int32))
            else:
                assert len(fixed_seeds)==2, "Supply a list with len = 2"
        
        # Recycling? There are requirements
        if recycle_img1 or recycle_img2:
            if self.list_nmb_branches_prev == []:
                print("Warning. You want to recycle but there is nothing here. Disabling recycling.")
                recycle_img1 = False
                recycle_img2 = False
            elif self.list_nmb_branches_prev != list_nmb_branches:
                print("Warning. Cannot change list_nmb_branches if recycling latent. Disabling recycling.")
                recycle_img1 = False
                recycle_img2 = False
            elif self.list_injection_idx_prev != list_injection_idx:
                print("Warning. Cannot change list_nmb_branches if recycling latent. Disabling recycling.")
                recycle_img1 = False
                recycle_img2 = False
        
        # Make a backup for future reference
        self.list_nmb_branches_prev = list_nmb_branches
        self.list_injection_idx_prev = list_injection_idx
        
        # Auto inits
        list_injection_idx_ext = list_injection_idx[:] 
        list_injection_idx_ext.append(self.num_inference_steps)
        
        # If injection at depth 0 not specified, we will start out with 2 branches
        if list_injection_idx_ext[0] != 0:
            list_injection_idx_ext.insert(0,0)
            list_nmb_branches.insert(0,2)
        assert list_nmb_branches[0] == 2, "Need to start with 2 branches. set list_nmb_branches[0]=2"
        
        # Pre-define entire branching tree structures
        if not recycle_img1 and not recycle_img2:
            self.tree_latents = []
            self.tree_fracts = []
            self.tree_status = []
            self.tree_final_imgs = [None]*list_nmb_branches[-1]
            self.tree_final_imgs_timing = [0]*list_nmb_branches[-1]
            
            nmb_blocks_time = len(list_injection_idx_ext)-1
            for t_block in range(nmb_blocks_time):
                nmb_branches = list_nmb_branches[t_block]
                list_fract_mixing_current = np.linspace(0, 1, nmb_branches)
                self.tree_fracts.append(list_fract_mixing_current)
                self.tree_latents.append([None]*nmb_branches)
                self.tree_status.append(['untouched']*nmb_branches)
        else:
            self.tree_final_imgs = [None]*list_nmb_branches[-1]
            nmb_blocks_time = len(list_injection_idx_ext)-1
            for t_block in range(nmb_blocks_time):
                nmb_branches = list_nmb_branches[t_block]
                for idx_branch in range(nmb_branches):
                    self.tree_status[t_block][idx_branch] = 'untouched'
                if recycle_img1:
                    self.tree_status[t_block][0] = 'computed'
                    self.tree_final_imgs[0] = self.latent2image(self.tree_latents[-1][0][-1])
                if recycle_img2:
                    self.tree_status[t_block][-1] = 'computed'
                    self.tree_final_imgs[-1] = self.latent2image(self.tree_latents[-1][-1][-1])
                    
        # setup compute order: goal: try to get last branch computed asap. 
        # first compute the right keyframe. needs to be there in any case
        list_compute = []
        list_local_stem = []
        for t_block in range(nmb_blocks_time - 1, -1, -1):
            if self.tree_status[t_block][0] == 'untouched':
                self.tree_status[t_block][0] = 'prefetched'
                list_local_stem.append([t_block, 0])
        list_compute.extend(list_local_stem[::-1]) 
        
        # setup compute order: start from last leafs (the final transition images) and work way down. what parents do they need?
        for idx_leaf in range(1, list_nmb_branches[-1]):
            list_local_stem = []
            t_block = nmb_blocks_time - 1
            t_block_prev = t_block - 1
            self.tree_status[t_block][idx_leaf] = 'prefetched'
            list_local_stem.append([t_block, idx_leaf])
            idx_leaf_deep = idx_leaf
            
            for t_block in range(nmb_blocks_time-1, 0, -1):
                t_block_prev = t_block - 1
                fract_mixing = self.tree_fracts[t_block][idx_leaf_deep]
                list_fract_mixing_prev = self.tree_fracts[t_block_prev]
                b_parent1, b_parent2 = get_closest_idx(fract_mixing, list_fract_mixing_prev)
                assert self.tree_status[t_block_prev][b_parent1] != 'untouched', 'Branch destruction??? This should never happen!'
                if self.tree_status[t_block_prev][b_parent2] == 'untouched':
                    self.tree_status[t_block_prev][b_parent2] = 'prefetched'
                    list_local_stem.append([t_block_prev, b_parent2])
                idx_leaf_deep = b_parent2
            list_compute.extend(list_local_stem[::-1])        
            
        # Diffusion computations start here
        time_start = time.time()
        for t_block, idx_branch in tqdm(list_compute, desc="computing transition"):
            # print(f"computing t_block {t_block} idx_branch {idx_branch}")
            idx_stop = list_injection_idx_ext[t_block+1]
            fract_mixing = self.tree_fracts[t_block][idx_branch]
            text_embeddings_mix = interpolate_linear(self.text_embedding1, self.text_embedding2, fract_mixing)
            if t_block == 0:
                if fixed_seeds is not None:
                    if idx_branch == 0:
                        self.set_seed(fixed_seeds[0])
                    elif idx_branch == list_nmb_branches[0] -1:
                        self.set_seed(fixed_seeds[1])
                list_latents = self.run_diffusion(text_embeddings_mix, idx_stop=idx_stop)
            else:
                # find parents latents
                b_parent1, b_parent2 = get_closest_idx(fract_mixing, self.tree_fracts[t_block-1])
                latents1 = self.tree_latents[t_block-1][b_parent1][-1]
                if fract_mixing == 0:
                    latents2 = latents1
                else:
                    latents2 = self.tree_latents[t_block-1][b_parent2][-1]
                idx_start = list_injection_idx_ext[t_block]
                fract_mixing_parental = (fract_mixing - self.tree_fracts[t_block-1][b_parent1]) / (self.tree_fracts[t_block-1][b_parent2] - self.tree_fracts[t_block-1][b_parent1]) 
                latents_for_injection = interpolate_spherical(latents1, latents2, fract_mixing_parental)
                list_latents = self.run_diffusion(text_embeddings_mix, latents_for_injection, idx_start=idx_start, idx_stop=idx_stop)
            
            self.tree_latents[t_block][idx_branch] = list_latents
            self.tree_status[t_block][idx_branch] = 'computed'
            
            # Convert latents to image directly for the last t_block
            if t_block == nmb_blocks_time-1:
                self.tree_final_imgs[idx_branch] = self.latent2image(list_latents[-1])
                self.tree_final_imgs_timing[idx_branch] = time.time() - time_start
            
        return self.tree_final_imgs
                

    @torch.no_grad()
    def run_diffusion(
            self, 
            text_embeddings: torch.FloatTensor, 
            latents_for_injection: torch.FloatTensor = None, 
            idx_start: int = -1, 
            idx_stop: int = -1, 
            return_image: Optional[bool] = False
        ):
        r"""
        Wrapper function for run_diffusion_standard and run_diffusion_inpaint.
        Depending on the mode, the correct one will be executed.
        
        Args:
            text_embeddings: torch.FloatTensor
                Text embeddings used for diffusion
            latents_for_injection: torch.FloatTensor 
                Latents that are used for injection
            idx_start: int
                Index of the diffusion process start and where the latents_for_injection are injected
            idx_stop: int
                Index of the diffusion process end.
            return_image: Optional[bool]
                Optionally return image directly
        """
        
        
        if self.mode == 'standard':
            return self.run_diffusion_standard(text_embeddings, latents_for_injection=latents_for_injection, idx_start=idx_start, idx_stop=idx_stop, return_image=return_image)
        
        elif self.mode == 'inpaint':
            assert self.image_source is not None, "image_source is None. Please run init_inpainting first."
            assert self.mask_image is not None, "image_source is None. Please run init_inpainting first."
            return self.run_diffusion_inpaint(text_embeddings, latents_for_injection=latents_for_injection, idx_start=idx_start, idx_stop=idx_stop, return_image=return_image)


    @torch.no_grad()
    def run_diffusion_standard(
            self, 
            text_embeddings: torch.FloatTensor, 
            latents_for_injection: torch.FloatTensor = None, 
            idx_start: int = -1, 
            idx_stop: int = -1, 
            return_image: Optional[bool] = False
        ):
        r"""
        Runs regular diffusion. Returns a list of latents that were computed.
        Adaptations allow to supply 
        a) starting index for diffusion
        b) stopping index for diffusion
        c) latent representations that are injected at the starting index
        Furthermore the intermittent latents are collected and returned.
        Adapted from diffusers (https://github.com/huggingface/diffusers)
        
        Args:
            text_embeddings: torch.FloatTensor
                Text embeddings used for diffusion
            latents_for_injection: torch.FloatTensor 
                Latents that are used for injection
            idx_start: int
                Index of the diffusion process start and where the latents_for_injection are injected
            idx_stop: int
                Index of the diffusion process end.
            return_image: Optional[bool]
                Optionally return image directly
                
        """ 
        if latents_for_injection is None:
            do_inject_latents = False
        else:
            do_inject_latents = True    
        
        generator = torch.Generator(device=self.device).manual_seed(int(self.seed))
        batch_size = 1
        height = self.height
        width = self.width
        num_inference_steps = self.num_inference_steps
        num_images_per_prompt = 1
        do_classifier_free_guidance = True
        
        # duplicate text embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_images_per_prompt, 1)
        text_embeddings = text_embeddings.view(bs_embed * num_images_per_prompt, seq_len, -1)
        
        # set timesteps
        self.pipe.scheduler.set_timesteps(num_inference_steps)
        
        # Some schedulers like PNDM have timesteps as arrays
        # It's more optimized to move all timesteps to correct device beforehand
        timesteps_tensor = self.pipe.scheduler.timesteps.to(self.pipe.device)
        
        if not do_inject_latents:
            # get the initial random noise unless the user supplied it
            latents_shape = (batch_size * num_images_per_prompt, self.pipe.unet.in_channels, height // 8, width // 8)
            latents_dtype = text_embeddings.dtype
            latents = torch.randn(latents_shape, generator=generator, device=self.pipe.device, dtype=latents_dtype)
            
            # scale the initial noise by the standard deviation required by the scheduler
            latents = latents * self.pipe.scheduler.init_noise_sigma
        extra_step_kwargs = {}

        # collect latents
        list_latents_out = []
        for i, t in enumerate(timesteps_tensor):
            
            
            if do_inject_latents:
                # Inject latent at right place
                if i < idx_start:
                    continue
                elif i == idx_start:
                    latents = latents_for_injection.clone()
            
            if i == idx_stop:
                return list_latents_out
            
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            latent_model_input = self.pipe.scheduler.scale_model_input(latent_model_input, t)
        
            # predict the noise residual
            noise_pred = self.pipe.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample
        
            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + self.guidance_scale * (noise_pred_text - noise_pred_uncond)
        
            # compute the previous noisy sample x_t -> x_t-1
            latents = self.pipe.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample
        
            list_latents_out.append(latents.clone())

        if return_image:        
            return self.latent2image(latents)
        else:
            return list_latents_out

    
    @torch.no_grad()
    def run_diffusion_inpaint(
            self, 
            text_embeddings: torch.FloatTensor, 
            latents_for_injection: torch.FloatTensor = None, 
            idx_start: int = -1, 
            idx_stop: int = -1, 
            return_image: Optional[bool] = False
        ):
        r"""
        Runs inpaint-based diffusion. Returns a list of latents that were computed.
        Adaptations allow to supply 
        a) starting index for diffusion
        b) stopping index for diffusion
        c) latent representations that are injected at the starting index
        Furthermore the intermittent latents are collected and returned.
        
        Adapted from diffusers (https://github.com/huggingface/diffusers)
        Args:
            text_embeddings: torch.FloatTensor
                Text embeddings used for diffusion
            latents_for_injection: torch.FloatTensor 
                Latents that are used for injection
            idx_start: int
                Index of the diffusion process start and where the latents_for_injection are injected
            idx_stop: int
                Index of the diffusion process end.
            return_image: Optional[bool]
                Optionally return image directly
                
        """ 
        
        if latents_for_injection is None:
            do_inject_latents = False
        else:
            do_inject_latents = True
        
        generator = torch.Generator(device=self.device).manual_seed(int(self.seed))
        batch_size = 1
        height = self.height
        width = self.width
        num_inference_steps = self.num_inference_steps
        num_images_per_prompt = 1
        do_classifier_free_guidance = True
        
        # prepare mask and masked_image
        mask, masked_image = self.prepare_mask_and_masked_image(self.image_source, self.mask_image)
        mask = mask.to(device=self.pipe.device, dtype=text_embeddings.dtype)
        masked_image = masked_image.to(device=self.pipe.device, dtype=text_embeddings.dtype)
    
        # resize the mask to latents shape as we concatenate the mask to the latents
        mask = torch.nn.functional.interpolate(mask, size=(height // 8, width // 8))
    
        # encode the mask image into latents space so we can concatenate it to the latents
        masked_image_latents = self.pipe.vae.encode(masked_image).latent_dist.sample(generator=generator)
        masked_image_latents = 0.18215 * masked_image_latents
    
        # duplicate mask and masked_image_latents for each generation per prompt, using mps friendly method
        mask = mask.repeat(num_images_per_prompt, 1, 1, 1)
        masked_image_latents = masked_image_latents.repeat(num_images_per_prompt, 1, 1, 1)
    
        mask = torch.cat([mask] * 2) if do_classifier_free_guidance else mask
        masked_image_latents = (
            torch.cat([masked_image_latents] * 2) if do_classifier_free_guidance else masked_image_latents
        )
    
        num_channels_mask = mask.shape[1]
        num_channels_masked_image = masked_image_latents.shape[1]
        
        num_channels_latents = self.pipe.vae.config.latent_channels
        latents_shape = (batch_size * num_images_per_prompt, num_channels_latents, height // 8, width // 8)
        latents_dtype = text_embeddings.dtype
        latents = torch.randn(latents_shape, generator=generator, device=self.pipe.device, dtype=latents_dtype)
        latents = latents.to(self.pipe.device)
        # set timesteps
        self.pipe.scheduler.set_timesteps(num_inference_steps)
        timesteps_tensor = self.pipe.scheduler.timesteps.to(self.pipe.device)
        latents = latents * self.pipe.scheduler.init_noise_sigma
        extra_step_kwargs = {}
        # collect latents
        list_latents_out = []
        
        for i, t in enumerate(timesteps_tensor):
            if do_inject_latents:
                # Inject latent at right place
                if i < idx_start:
                    continue
                elif i == idx_start:
                    latents = latents_for_injection.clone()
            
            if i == idx_stop:
                return list_latents_out
                    
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            # concat latents, mask, masked_image_latents in the channel dimension
            latent_model_input = torch.cat([latent_model_input, mask, masked_image_latents], dim=1)
    
            latent_model_input = self.pipe.scheduler.scale_model_input(latent_model_input, t)
    
            # predict the noise residual
            noise_pred = self.pipe.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample
        
            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + self.guidance_scale * (noise_pred_text - noise_pred_uncond)
        
            # compute the previous noisy sample x_t -> x_t-1
            latents = self.pipe.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample
        
            list_latents_out.append(latents.clone())
        
        if return_image:        
            return self.latent2image(latents)
        else:
            return list_latents_out
        
    @torch.no_grad()
    def latent2image(
            self, 
            latents: torch.FloatTensor
        ):
        r"""
        Returns an image provided a latent representation from diffusion.
        Args:
            latents: torch.FloatTensor
                Result of the diffusion process. 
        """
        
        latents = 1 / 0.18215 * latents
        image = self.pipe.vae.decode(latents).sample
        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).float().numpy()
        image = (image[0,:,:,:] * 255).astype(np.uint8)
        
        return image
   
    @torch.no_grad()
    def get_text_embeddings(
            self, 
            prompt: str
        ):
        r"""
        Computes the text embeddings provided a string with a prompts.
        Adapted from diffusers (https://github.com/huggingface/diffusers)
        Args:
            prompt: str
                ABC trending on artstation painted by Old Greg.
        """        
        uncond_tokens = [""]
        batch_size = 1
        num_images_per_prompt = 1
        do_classifier_free_guidance = True
        # get prompt text embeddings
        text_inputs = self.pipe.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.pipe.tokenizer.model_max_length,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        
        # if text_input_ids.shape[-1] > self.pipe.tokenizer.modeLatentBlendingl_max_length:
        #     removed_text = self.pipe.tokenizer.batch_decode(text_input_ids[:, self.pipe.tokenizer.model_max_length :])
        #     text_input_ids = text_input_ids[:, : self.pipe.tokenizer.model_max_length]
        text_embeddings = self.pipe.text_encoder(text_input_ids.to(self.pipe.device))[0]
        
        # duplicate text embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_images_per_prompt, 1)
        text_embeddings = text_embeddings.view(bs_embed * num_images_per_prompt, seq_len, -1)
        
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            max_length = text_input_ids.shape[-1]
            uncond_input = self.pipe.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pt",
            )
            uncond_embeddings = self.pipe.text_encoder(uncond_input.input_ids.to(self.pipe.device))[0]
        
            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = uncond_embeddings.repeat(batch_size, num_images_per_prompt, 1)
            uncond_embeddings = uncond_embeddings.view(batch_size * num_images_per_prompt, seq_len, -1)
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])
        return text_embeddings
    
    
    def prepare_mask_and_masked_image(self, image, mask):
        r"""
        Mask and image preparation for inpainting. 
        Adapted from diffusers (https://github.com/huggingface/diffusers)
        Args:
            image: 
                Source image
            mask: 
                Mask image
        """ 
        image = np.array(image.convert("RGB"))
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image).to(dtype=torch.float32) / 127.5 - 1.0
    
        mask = np.array(mask.convert("L"))
        mask = mask.astype(np.float32) / 255.0
        mask = mask[None, None]
        mask[mask < 0.5] = 0
        mask[mask >= 0.5] = 1
        mask = torch.from_numpy(mask)
    
        masked_image = image * (mask < 0.5)
    
        return mask, masked_image

    def randomize_seed(self):
        r"""
        Set a random seed for a fresh start.
        """ 
        seed = np.random.randint(999999999)
        self.set_seed(seed)
    
    def set_seed(self, seed: int):
        r"""
        Set a the seed for a fresh start.
        """ 
        self.seed = seed
        

    def swap_forward(self):
        r"""
        Moves over keyframe two -> keyframe one. Useful for making a sequence of transitions.
        """ 
        # Move over all latents
        for t_block in range(len(self.tree_latents)):
            self.tree_latents[t_block][0] = self.tree_latents[t_block][-1]
        
        # Move over prompts and text embeddings
        self.prompt1 = self.prompt2
        self.text_embedding1 = self.text_embedding2
        
        # Final cleanup for extra sanity
        self.tree_final_imgs = [] 
        
# Auxiliary functions
def get_closest_idx(
        fract_mixing: float, 
        list_fract_mixing_prev: List[float],
    ):
    r"""
    Helper function to retrieve the parents for any given mixing.
    Example: fract_mixing = 0.4 and list_fract_mixing_prev = [0, 0.3, 0.6, 1.0]
    Will return the two closest values from list_fract_mixing_prev, i.e. [1, 2]
    """ 
        
    pdist = fract_mixing - np.asarray(list_fract_mixing_prev)
    pdist_pos = pdist.copy()
    pdist_pos[pdist_pos<0] = np.inf
    b_parent1 = np.argmin(pdist_pos)
    pdist_neg = -pdist.copy()
    pdist_neg[pdist_neg<=0] = np.inf
    b_parent2= np.argmin(pdist_neg)
    
    if b_parent1 > b_parent2:
        tmp = b_parent2
        b_parent2 = b_parent1
        b_parent1 = tmp
    
    return b_parent1, b_parent2

@torch.no_grad()
def interpolate_spherical(p0, p1, fract_mixing: float):
    r"""
    Helper function to correctly mix two random variables using spherical interpolation.
    See https://en.wikipedia.org/wiki/Slerp
    The function will always cast up to float64 for sake of extra precision.
    Args:
        p0: 
            First tensor for interpolation
        p1: 
            Second tensor for interpolation
        fract_mixing: float 
            Mixing coefficient of interval [0, 1]. 
            0 will return in p0
            1 will return in p1
            0.x will return a mix between both preserving angular velocity.
    """ 
    
    if p0.dtype == torch.float16:
        recast_to = 'fp16'
    else:
        recast_to = 'fp32'
    
    p0 = p0.double()
    p1 = p1.double()
    norm = torch.linalg.norm(p0) * torch.linalg.norm(p1)
    epsilon = 1e-7
    dot = torch.sum(p0 * p1) / norm
    dot = dot.clamp(-1+epsilon, 1-epsilon)
    
    theta_0 = torch.arccos(dot)
    sin_theta_0 = torch.sin(theta_0)
    theta_t = theta_0 * fract_mixing
    s0 = torch.sin(theta_0 - theta_t) / sin_theta_0
    s1 = torch.sin(theta_t) / sin_theta_0
    interp = p0*s0 + p1*s1
    
    if recast_to == 'fp16':
        interp = interp.half()
    elif recast_to == 'fp32':
        interp = interp.float()
        
    return interp


def interpolate_linear(p0, p1, fract_mixing):
    r"""
    Helper function to mix two variables using standard linear interpolation.
    Args:
        p0: 
            First tensor for interpolation
        p1: 
            Second tensor for interpolation
        fract_mixing: float 
            Mixing coefficient of interval [0, 1]. 
            0 will return in p0
            1 will return in p1
            0.x will return a linear mix between both.
    """ 
    return (1-fract_mixing) * p0 + fract_mixing * p1


def add_frames_linear_interp(
        list_imgs: List[np.ndarray], 
        fps_target: Union[float, int] = None, 
        duration_target: Union[float, int] = None,
        nmb_frames_target: int=None,
    ):
    r"""
    Helper function to cheaply increase the number of frames given a list of images, 
    by virtue of standard linear interpolation.
    The number of inserted frames will be automatically adjusted so that the total of number
    of frames can be fixed precisely, using a random shuffling technique.
    The function allows 1:1 comparisons between transitions as videos.
    
    Args:
        list_imgs: List[np.ndarray)
            List of images, between each image new frames will be inserted via linear interpolation.
        fps_target: 
            OptionA: specify here the desired frames per second.
        duration_target: 
            OptionA: specify here the desired duration of the transition in seconds.
        nmb_frames_target: 
            OptionB: directly fix the total number of frames of the output.
    """ 
    
    # Sanity
    if nmb_frames_target is not None and fps_target is not None:
        raise ValueError("You cannot specify both fps_target and nmb_frames_target")
    if fps_target is None:
        assert nmb_frames_target is not None, "Either specify nmb_frames_target or nmb_frames_target"
    if nmb_frames_target is None:
        assert fps_target is not None, "Either specify duration_target and fps_target OR nmb_frames_target"
        assert duration_target is not None, "Either specify duration_target and fps_target OR nmb_frames_target"
        nmb_frames_target = fps_target*duration_target
    
    # Get number of frames that are missing
    nmb_frames_diff = len(list_imgs)-1
    nmb_frames_missing = nmb_frames_target - nmb_frames_diff - 1
    
    if nmb_frames_missing < 1:
        return list_imgs
    
    list_imgs_float = [img.astype(np.float32) for img in list_imgs]
    
    # Distribute missing frames, append nmb_frames_to_insert(i) frames for each frame
    mean_nmb_frames_insert = nmb_frames_missing/nmb_frames_diff
    constfact = np.floor(mean_nmb_frames_insert)
    remainder_x = 1-(mean_nmb_frames_insert - constfact)
    
    nmb_iter = 0
    while True:
        nmb_frames_to_insert = np.random.rand(nmb_frames_diff)
        nmb_frames_to_insert[nmb_frames_to_insert<=remainder_x] = 0
        nmb_frames_to_insert[nmb_frames_to_insert>remainder_x] = 1
        nmb_frames_to_insert += constfact
        if np.sum(nmb_frames_to_insert) == nmb_frames_missing:
            break
        nmb_iter += 1
        if nmb_iter > 100000:
            print("add_frames_linear_interp: issue with inserting the right number of frames")
            break
        
    nmb_frames_to_insert = nmb_frames_to_insert.astype(np.int32)
    list_imgs_interp = []
    for i in tqdm(range(len(list_imgs_float)-1), desc="STAGE linear interp"):
        img0 = list_imgs_float[i]
        img1 = list_imgs_float[i+1]
        list_imgs_interp.append(img0.astype(np.uint8))
        list_fracts_linblend = np.linspace(0, 1, nmb_frames_to_insert[i]+2)[1:-1]
        for fract_linblend in list_fracts_linblend:
            img_blend = interpolate_linear(img0, img1, fract_linblend).astype(np.uint8)
            list_imgs_interp.append(img_blend.astype(np.uint8))
        
        if i==len(list_imgs_float)-2:
            list_imgs_interp.append(img1.astype(np.uint8))
    
    return list_imgs_interp


def get_time(resolution=None):
    """
    Helper function returning an nicely formatted time string, e.g. 221117_1620
    """
    if resolution==None:
        resolution="second"
    if resolution == "day":
        t = time.strftime('%y%m%d', time.localtime())
    elif resolution == "minute":
        t = time.strftime('%y%m%d_%H%M', time.localtime())
    elif resolution == "second":
        t = time.strftime('%y%m%d_%H%M%S', time.localtime())
    elif resolution == "millisecond":
        t = time.strftime('%y%m%d_%H%M%S', time.localtime())
        t += "_"
        t += str("{:03d}".format(int(int(datetime.utcnow().strftime('%f'))/1000)))
    else:
        raise ValueError("bad resolution provided: %s" %resolution)
    return t

#%% le main
if __name__ == "__main__":
    

        


    #%%
    """
    TODO Coding:
        RUNNING WITHOUT PROMPT!
        
        auto mode (quality settings)
        save value ranges, can it be trashed?
        set all variables in init! self.img2...
        
    TODO Other:
        github
        write text
        requirements
        make graphic explaining
        make colab
        license
        twitter et al
    """
