import torch
from PIL import Image
import numpy
import sys
from torchvision import transforms
import numpy as np
import cv2

def grad_rollout(attentions, gradients, discard_ratio, num_classes, class_idx, distillation):
    if distillation:
        skip_idx = 196 + num_classes*2
        mask_idx = num_classes*2
    else:
        skip_idx = 196 + num_classes
        mask_idx = num_classes
    result = torch.eye(attentions[0].size(-1))
    with torch.no_grad():
        for attention, grad in zip(attentions, gradients):                
            weights = grad
            attention_heads_fused = (attention*weights).mean(axis=1)
            attention_heads_fused[attention_heads_fused < 0] = 0

            # Drop the lowest attentions, but
            # don't drop the class token
            flat = attention_heads_fused.view(attention_heads_fused.size(0), -1)
            _, indices = flat.topk(int(flat.size(-1)*discard_ratio), -1, False)
            #indices = indices[indices != 0]
            avoid = []
            for i in range(num_classes):
                for j in range(num_classes):
                    avoid.append(skip_idx*i + j)
            avoid = torch.tensor(avoid, dtype=torch.uint8)
            indices = indices[~torch.isin(indices, avoid)]
            flat[0, indices] = 0

            I = torch.eye(attention_heads_fused.size(-1))
            a = (attention_heads_fused + 1.0*I)/2
            a = a / a.sum(dim=-1)
            result = torch.matmul(a, result)
    
    # Look at the total attention between the class token,
    # and the image patches
    mask = result[0, class_idx , mask_idx:]
    # In case of 224x224 image, this brings us from 196 to 14
    width = int(mask.size(-1)**0.5)
    mask = mask.reshape(width, width).numpy()
    mask = mask / np.max(mask)
    return mask    

class VITAttentionGradRollout:
    def __init__(self, model, num_classes=4, distillation_token=False, attention_layer_name='attn_drop',
        discard_ratio=0.9, device="cuda"):
        self.model = model
        self.discard_ratio = discard_ratio
        self.num_classes = num_classes
        self.distillation = distillation_token
        self.device = device
        for name, module in self.model.named_modules():
            if attention_layer_name in name:
                module.register_forward_hook(self.get_attention)
                module.register_full_backward_hook(self.get_attention_gradient)

        self.attentions = []
        self.attention_gradients = []

    def get_attention(self, module, input, output):
        self.attentions.append(output.cpu())

    def get_attention_gradient(self, module, grad_input, grad_output):
        self.attention_gradients.append(grad_input[0].cpu())

    def reset_lists(self):
        self.attentions = []
        self.attention_gradients = []

    def __call__(self, input_tensor, category_index):
        self.attentions = []
        self.attention_gradients = []
        self.model.zero_grad()
        output = self.model(input_tensor)
        category_mask = torch.zeros(output.size(), device=self.device)
        category_mask[:, category_index] = 1
        loss = (output*category_mask).sum()
        loss.backward()

        return grad_rollout(self.attentions, self.attention_gradients,
            self.discard_ratio, num_classes=self.num_classes, class_idx=category_index, distillation=self.distillation)