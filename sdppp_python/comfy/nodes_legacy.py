import numpy as np
import json
import torch
import time
import node_helpers
from io import BytesIO
from PIL import Image, ImageOps, ImageSequence, ImageFile
from nodes import CLIPTextEncode, ConditioningConcat, ConditioningSetMask
from ..apis import addImageCache
from ..protocols.photoshop import ProtocolPhotoshop
from .nodes import check_linked_in_prompt, sdppp_get_prompt_item_from_list

def define_comfyui_nodes_legacy(sdppp):
    def validate_sdppp():
        if not sdppp.has_ps_instance():
            return 'Photoshop is not connected'
        return True

    def call_async_func_in_server_thread(coro, dontwait = False):
        handle = {
            'done': False,
            'result': None,
            'error': None
        }
        loop = sdppp.loop
        async def do_call():
            try: 
                handle['result'] = await coro
            except Exception as e:
                handle['error'] = e
            finally:
                handle['done'] = True
        loop.create_task(do_call())

        if not dontwait:
            while not handle['done']:
                pass
            if handle['error'] is not None:
                raise handle['error']
            else:
                return handle['result']
        else:
            return None

    def parse_params(unique_id, prompt, layer_or_group, document=""):
        linked_style = check_linked_in_prompt(prompt, unique_id, 'layer_or_group')
        if not linked_style:
            document = json.loads(document[0])
        else:
            document = layer_or_group[0]['document']
        return linked_style, document


    class GetImageFromPhotoshopLayerNode:
        RETURN_TYPES = ("IMAGE", "MASK")
        RETURN_NAMES = ("rgb_out", "alpha_out")
        OUTPUT_IS_LIST = (True, True)
        INPUT_IS_LIST = True
        FUNCTION = "get_image"
        CATEGORY = "SD-PPP"

        @classmethod
        def VALIDATE_INPUTS(s):
            return validate_sdppp()
        
        @classmethod
        def IS_CHANGED(self, unique_id, prompt, layer_or_group, bound="", document="", selection_only=False):
            linked_style, document = parse_params(unique_id, prompt, layer_or_group, document)

            if ('instance_id' not in document) or (document['instance_id'] not in sdppp.backend_instances):
                return np.random.rand()

            return sdppp.backend_instances[document['instance_id']].data['canvasStateID']
        
        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "layer_or_group": ('LAYER', {"default": None}),
                    "selection_only": ("STRING", {"default": "false", "sdppp_type": "LAYER_selection_only"}),
                },
                "optional": {
                    # compat combo selection type
                    "document": ("STRING", {"default": "", "sdppp_type": "DOCUMENT_nameid"}),
                    "bound": ('BOUND', {"default": None}),
                },
                "hidden": {
                    "unique_id": "UNIQUE_ID",
                    "prompt": "PROMPT", 
                }
            }

        def get_image(self, unique_id, prompt, layer_or_group, bound="", document="", selection_only=False):
            if validate_sdppp() is not True:
                raise ValueError('Photoshop is not connected')

            linked_style, document = parse_params(unique_id, prompt, layer_or_group, document)
            if document['instance_id'] not in sdppp.backend_instances:
                raise ValueError(f'Photoshop instance {document["instance_id"]} not found')

            res_image = []
            res_mask = []
            startTime = time.time()
            for i, item_layer in enumerate(layer_or_group):
                if linked_style:
                    item_layer = item_layer['layer_identify']
                item_bound = sdppp_get_prompt_item_from_list(bound, i)
                item_selection_only = sdppp_get_prompt_item_from_list(selection_only, i)
                start = time.time()
                result = call_async_func_in_server_thread(
                    ProtocolPhotoshop.get_image(
                        backend_instance=sdppp.backend_instances[document['instance_id']], 
                        document_identify=document['identify'], 
                        layer_identify=item_layer, 
                        bound_identify=item_bound,
                        selection_only=(item_selection_only==True or item_selection_only=="True")
                    )
                )
                (output_image, output_mask) = self._load_image(
                    [result['blob']], 
                    [result['width']], [result['height']], 
                    [len(result['blob']) / (result['width'] * result['height'])]
                )

                res_image.append(output_image)
                res_mask.append(output_mask)

            return (res_image, res_mask,)

        # copy from Comfyui/nodes.py LoadImage
        def _load_image(self, imagebuffers, widths, heights, components):
            output_images = []
            output_masks = []
            w, h = None, None

            excluded_formats = ['MPO']
            
            # for i in imagebuffers:
            for i, width, height in zip(imagebuffers, widths, heights):
                if (components[0] == 1):
                    image_mode = "L"
                elif (components[0] == 3):
                    image_mode = "RGB"
                elif (components[0] == 4):
                    image_mode = "RGBA"
                else:
                    raise ValueError("Unsupported number of components")

                i = Image.frombytes(image_mode, (width, height), i, "raw")
                
                if i.mode == 'I':
                    i = i.point(lambda i: i * (1 / 255))
                image = i.convert("RGB")

                if len(output_images) == 0:
                    w = image.size[0]
                    h = image.size[1]
                
                if image.size[0] != w or image.size[1] != h:
                    continue
                
                image = np.array(image).astype(np.float32) / 255.0
                image = torch.from_numpy(image)[None,]
                if 'A' in i.getbands():
                    mask = np.array(i.getchannel('A')).astype(np.float32) / 255.0
                    mask = 1. - torch.from_numpy(mask)
                else:
                    mask = torch.zeros((64,64), dtype=torch.float32, device="cpu")
                output_images.append(image)
                output_masks.append(mask.unsqueeze(0))

            if len(output_images) > 1 and img.format not in excluded_formats:
                output_image = torch.cat(output_images, dim=0)
                output_mask = torch.cat(output_masks, dim=0)
            else:
                output_image = output_images[0]
                output_mask = output_masks[0]

            return (output_image, output_mask)


    class SendImageToPhotoshopLayerNode:
        RETURN_TYPES = ()
        INPUT_IS_LIST = True
        FUNCTION = "send_image"
        CATEGORY = "SD-PPP"
        OUTPUT_NODE = True

        @classmethod
        def VALIDATE_INPUTS(s):
            return validate_sdppp()
        
        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "images": ("IMAGE", ),
                    "layer_or_group": ('LAYER', {"default": None}),
                },
                "optional": {
                    # compat combo selection type
                    "document": ("STRING", {"default": "", "sdppp_type": "DOCUMENT_nameid"}),
                    "bound": ('BOUND', {"default": None}),
                },
                "hidden": {
                    "unique_id": "UNIQUE_ID",
                    "prompt": "PROMPT", 
                }
            }

        def send_image(self, unique_id, prompt, images, layer_or_group, bound="", document=""):
            if validate_sdppp() is not True:
                raise ValueError('Photoshop is not connected')

            linked_style, document = parse_params(unique_id, prompt, layer_or_group, document)

            if document['instance_id'] not in sdppp.backend_instances:
                raise ValueError(f'Photoshop instance {document["instance_id"]} not found')

            params = []
            # iterate layer_or_group
            # diff between batch image and list is not known yet 
            for index, image in enumerate(images[0]):
                if len(layer_or_group) == 1:
                    item_layer = layer_or_group[0]
                else:
                    item_layer = layer_or_group[index]
                item_bound = sdppp_get_prompt_item_from_list(bound, index)

                if linked_style:
                    item_layer = item_layer['layer_identify']
                    
                i = 255. * image.cpu().numpy()
                img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                params.append({
                    'layer_identify': item_layer, 
                    'bound_identify': item_bound, 
                    'image_blob': {
                        'buffer': img.tobytes('raw'),
                        'width': img.width,
                        'height': img.height
                    }
                })

            call_async_func_in_server_thread(ProtocolPhotoshop.send_images(
                backend_instance=sdppp.backend_instances[document['instance_id']],
                document_identify=document['identify'], 
                image_blobs=[p['image_blob'] for p in params],  
                layer_identifies=[p['layer_identify'] for p in params], 
                bounds_identify=[p['bound_identify'] for p in params]
            ), True)

            return (None,)
        
    class ImageTimesOpacity:
        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "images": ("IMAGE", ),
                    "opacity": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 1.0, "step": 0.01}),
                }
            }

        RETURN_TYPES = ("IMAGE",)
        FUNCTION = "image_times_opacity"
        CATEGORY = "Photoshop"

        def image_times_opacity(self, images, opacity):
            image_out = images * opacity
            return (image_out,)
        
    class MaskTimesOpacity:
        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "masks": ("MASK", ),
                    "opacity": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 1.0, "step": 0.01}),
                }
            }

        RETURN_TYPES = ("MASK",)
        FUNCTION = "mask_times_opacity"
        CATEGORY = "Photoshop"

        def mask_times_opacity(self, masks, opacity):
            mask_out = masks * opacity
            return (mask_out,)

    class CLIPTextEncodePSRegional:
        @classmethod
        def INPUT_TYPES(s):
            return {
                "required": {
                    "clip": ("CLIP", {"tooltip": "The CLIP model used for encoding the text. only use the first one."}),
                    "texts": ("STRING", {"forceInput": True, "multiline": True, "dynamicPrompts": True, "tooltip": "The text to be encoded."}), 
                    "masks": ("MASK", )
                },
                "optional": {
                    "strengths": ("FLOAT", {"forceInput": True, "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                }
            }
        RETURN_TYPES = ("CONDITIONING",)
        FUNCTION = "encode"
        INPUT_IS_LIST = True

        CATEGORY = "Photoshop"

        def encode(self, clip, texts, masks, strengths=[]):
            clip = clip[0]

            ret = None
            for i in range(len(texts)):
                text = texts[i]
                mask = masks[i]
                if i in strengths:
                    strength = strengths[i]
                else:
                    strength = 1.0

                tokens = clip.tokenize(text)
                output = clip.encode_from_tokens(tokens, return_pooled=True, return_dict=True)
                cond = output.pop("cond")

                set_area_to_bounds = False
                if len(mask.shape) < 3:
                    mask = mask.unsqueeze(0)

                c = node_helpers.conditioning_set_values([[cond, output]], {"mask": 1.0 - mask,
                                                                        "set_area_to_bounds": set_area_to_bounds,
                                                                        "mask_strength": strength})
                if ret is None:
                    ret = c
                else:
                    ret = ret + c                                               
            return (ret, )
        
    return {
        'GetImageFromPhotoshopLayerNode': GetImageFromPhotoshopLayerNode,
        'SendImageToPhotoshopLayerNode': SendImageToPhotoshopLayerNode,
        'ImageTimesOpacity': ImageTimesOpacity,
        'MaskTimesOpacity': MaskTimesOpacity,
        'CLIPTextEncodePSRegional': CLIPTextEncodePSRegional
    }