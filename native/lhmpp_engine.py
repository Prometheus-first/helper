"""Pinned LHM++ body-avatar inference. Imported only in its Linux GPU worker."""
from pathlib import Path
import gc
import json
import math
import os
import subprocess
import sys
import time

COMMIT = '906b5d9fb967ab42efb92f6fa55bf22cac86b653'


class BodyAvatar:
    def __init__(self, repository, source_photo, *, size=420):
        root = Path(repository).resolve(strict=True)
        if subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() != COMMIT:
            raise RuntimeError('unsupported_lhmpp_revision')
        if type(size) is not int or not 140 <= size <= 560 or size % 14:
            raise ValueError('Invalid body renderer size')
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
        os.environ['HF_HUB_OFFLINE']='1'
        os.environ['TRANSFORMERS_OFFLINE']='1'
        import torch
        import cv2
        import numpy as np
        from PIL import Image
        from accelerate import Accelerator
        from core.models.encoders.dinov2_wrapper import Dinov2Wrapper
        from core.models import model_dict
        from core.utils.hf_hub import wrap_model_hub
        self.torch, self.np, self.cv2 = torch, np, cv2
        if not torch.cuda.is_available():
            raise RuntimeError('lhmpp_cuda_unavailable')
        torch._dynamo.config.disable = True
        torch.set_num_threads(4)
        Accelerator()
        # The complete encoder is in the pinned checkpoint; do not download
        # a duplicate backbone during its constructor.
        original = Dinov2Wrapper._build_dinov2
        Dinov2Wrapper._build_dinov2 = staticmethod(lambda model_name, modulation_dim=None, pretrained=True:
            original(model_name, modulation_dim, pretrained=False))

        class InferenceModel(model_dict['human_lrm_a4o']):
            def __init__(self, **options):
                options['use_face_id'] = False  # training-only identity loss
                super().__init__(**options)
                if type(self.neural_renderer).__name__ != 'PatchDPT4DecoderOnly':
                    raise RuntimeError('unsupported_lhmpp_decoder')
                # This decoder's forward never calls the inherited blocks;
                # their weights are intentionally absent from the release.
                self.neural_renderer.transformer_block = torch.nn.ModuleList()

        try:
            model = wrap_model_hub(InferenceModel).from_pretrained(
                str(root / 'pretrained_models/3DAIGC/LHMPP-700M'), strict=True)
        finally:
            Dinov2Wrapper._build_dinov2 = staticmethod(original)
        model.eval()
        model.cuda()
        self.model = model
        self.size = size
        intr = torch.eye(4, device='cuda').reshape(1,1,4,4)
        intr[0,0,0,0] = intr[0,0,1,1] = size
        intr[0,0,0,2] = intr[0,0,1,2] = size / 2
        self.c2w = torch.eye(4, device='cuda').reshape(1,1,4,4)
        self.intr, self.bg = intr, torch.ones(1,1,3,device='cuda')
        sp = {'betas':torch.zeros(1,10), 'root_pose':torch.zeros(1,1,3),
              'body_pose':torch.zeros(1,1,21,3), 'jaw_pose':torch.zeros(1,1,3),
              'leye_pose':torch.zeros(1,1,3), 'reye_pose':torch.zeros(1,1,3),
              'lhand_pose':torch.zeros(1,1,15,3), 'rhand_pose':torch.zeros(1,1,15,3),
              'trans':torch.tensor([[[0.,.25,2.4]]]), 'expr':torch.zeros(1,1,100),
              'focal':torch.tensor([[[size,size]]]).float(),
              'princpt':torch.tensor([[[size/2,size/2]]]),
              'img_size_wh':torch.tensor([[[size,size]]]).float()}
        self.sp = {k:v.cuda() for k,v in sp.items()}
        image = np.asarray(Image.open(source_photo).convert('RGB'))
        h,w = image.shape[:2]
        if h*w > 32000000:
            raise ValueError('Source image exceeds body-avatar pixel limit')
        factor = min(840/h,504/w)
        scaled = cv2.resize(image,(max(1,round(w*factor)),max(1,round(h*factor))))
        board = np.full((840,504,3),255,np.uint8)
        y,x = (840-scaled.shape[0])//2,(504-scaled.shape[1])//2
        board[y:y+scaled.shape[0],x:x+scaled.shape[1]] = scaled
        reference = torch.from_numpy(board.astype(np.float32)/255).permute(2,0,1)[None,None].cuda()
        # FP16 produced non-finite appearance tensors on the tested 4060.
        # Keep the verified full-precision path and reject invalid avatars.
        with torch.no_grad():
            outputs = model.infer_single_view(reference,None,None,
                render_c2ws=self.c2w,render_intrs=self.intr,render_bg_colors=self.bg,
                smplx_params=self.sp,ref_imgs_bool=torch.ones((1,1),dtype=torch.bool,device='cuda'))
        if len(outputs) != 7:
            raise RuntimeError('unsupported_lhmpp_output')
        self.gs,self.points,neutral,self.features,self.image_latents,self.motion_emb,self.pos_emb = outputs
        for appearance in self.gs:
            for value in vars(appearance).values():
                if isinstance(value,torch.Tensor) and not torch.isfinite(value).all():
                    raise RuntimeError('lhmpp_nonfinite_avatar')
        self.sp['transform_mat_neutral_pose'] = neutral
        layer = model.renderer.smplx_model.smplx_layer
        with torch.no_grad():
            rest = layer(betas=torch.zeros(1,10,device='cuda'),
                global_orient=torch.zeros(1,3,device='cuda'),body_pose=torch.zeros(1,63,device='cuda'),
                left_hand_pose=torch.zeros(1,45,device='cuda'),right_hand_pose=torch.zeros(1,45,device='cuda'),
                jaw_pose=torch.zeros(1,3,device='cuda'),leye_pose=torch.zeros(1,3,device='cuda'),
                reye_pose=torch.zeros(1,3,device='cuda'),expression=torch.zeros(1,100,device='cuda'),
                transl=torch.zeros(1,3,device='cuda'))
        self.rest_joints = rest.joints[0,:22].cpu().tolist()
        del outputs, reference, rest
        # Avatar appearance is now fixed; subsequent frames only deform and
        # render it. Release the reconstruction encoders from GPU memory.
        model.encoder = None
        model.transformer = None
        gc.collect()
        torch.cuda.empty_cache()

    def render(self, body_pose, root_delta=(0.,0.,0.)):
        torch,np = self.torch,self.np
        pose = np.asarray(body_pose,dtype=np.float32)
        delta = np.asarray(root_delta,dtype=np.float32)
        if (pose.shape != (21,3) or delta.shape != (3,) or not np.isfinite(pose).all() or
                not np.isfinite(delta).all() or np.abs(pose).max()>3.2 or np.abs(delta).max()>1.6):
            raise ValueError('Invalid body pose')
        from pytorch3d.transforms import axis_angle_to_matrix, matrix_to_axis_angle
        with torch.no_grad():
            self.sp['body_pose'].copy_(torch.from_numpy(pose).cuda()[None,None])
            base = axis_angle_to_matrix(torch.tensor([math.pi,0.,0.],device='cuda'))
            rotation = base @ axis_angle_to_matrix(torch.from_numpy(delta).cuda())
            self.sp['root_pose'].copy_(matrix_to_axis_angle(rotation)[None,None])
            started = time.perf_counter()
            rgb,mask = self.model.animation_infer(gs_model_list=self.gs,query_points=self.points,
                smplx_params=self.sp,render_c2ws=self.c2w,render_intrs=self.intr,render_bg_colors=self.bg,
                gs_hidden_features=self.features,image_latents=self.image_latents,
                motion_emb=self.motion_emb,pos_emb=self.pos_emb)
            if not torch.isfinite(rgb).all():
                raise RuntimeError('lhmpp_nonfinite_frame')
            frame = (rgb[0].clamp(0,1)*255).to(torch.uint8).cpu().numpy()
            elapsed = (time.perf_counter()-started)*1000
        return frame,elapsed
