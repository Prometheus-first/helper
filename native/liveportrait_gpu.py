"""Keep LivePortrait's two 5-D grid samples on CUDA with PyTorch.

The unchanged ONNX graph is split at its GridSample boundaries. Intermediate
tensors use CUDA I/O binding and never travel through host NumPy arrays.
"""
from pathlib import Path
import hashlib
import json


def split_warp(model_path, *, half=True):
    import onnx
    from onnx.utils import Extractor
    path = Path(model_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache = path.parent / ('.helper-gpu-v3-' + ('fp16-' if half else 'fp32-') + digest[:16])
    manifest_path = cache / 'manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('source_sha256') == digest and all(
                (cache / item['path']).is_file() and
                hashlib.sha256((cache / item['path']).read_bytes()).hexdigest() == item['sha256']
                for item in manifest['parts']):
            return cache, manifest
    cache.mkdir(parents=True, exist_ok=True)
    model = onnx.shape_inference.infer_shapes(onnx.load(path))
    grids = [n for n in model.graph.node if n.op_type == 'GridSample']
    if len(grids) != 2:
        raise RuntimeError('unsupported_portrait_warp_graph')
    original_inputs = [x.name for x in model.graph.input]
    available = original_inputs[:]
    extractor = Extractor(model)
    parts = []
    nodes = list(model.graph.node)
    boundaries = [i for i, node in enumerate(nodes) if node.op_type == 'GridSample']
    start = 0
    for index in range(3):
        end = boundaries[index] if index < 2 else len(nodes)
        future_inputs = {name for node in nodes[end:] for name in node.input}
        future_inputs.update(x.name for x in model.graph.output)
        outputs = [name for node in nodes[start:end] for name in node.output if name in future_inputs]
        # A requested grid input can also be an existing model input. It
        # already lives on the GPU and need not pass through a no-op graph.
        outputs = [name for name in outputs if name not in available]
        part = extractor.extract_model(available, outputs)
        if half:
            from onnxruntime.transformers.float16 import convert_float_to_float16
            part = convert_float_to_float16(part, keep_io_types=False, disable_shape_infer=True)
        onnx.checker.check_model(part)
        filename = f'warp-{index}.onnx'
        onnx.save(part, cache / filename)
        parts.append({'path': filename, 'sha256': hashlib.sha256((cache / filename).read_bytes()).hexdigest()})
        available.extend(outputs)
        if index < 2:
            available.extend(grids[index].output)
            start = boundaries[index] + 1
    manifest = {'source_sha256': digest, 'parts': parts,
                'inputs': original_inputs, 'output': model.graph.output[0].name,
                'grids': [{'inputs': list(n.input), 'output': n.output[0],
                           'attributes': {a.name: (onnx.helper.get_attribute_value(a).decode() if a.type == onnx.AttributeProto.STRING
                                                 else onnx.helper.get_attribute_value(a)) for a in n.attribute}}
                          for n in grids]}
    manifest_path.write_text(json.dumps(manifest), encoding='utf8')
    return cache, manifest


class TorchGridWarp:
    def __init__(self, model_path, *, half=True):
        import onnxruntime as ort
        import torch
        self.torch = torch
        self.dtype = torch.float16 if half else torch.float32
        cache, self.manifest = split_warp(model_path, half=half)
        self.sessions = []
        self.buffers = []
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.intra_op_num_threads = 2
        for part in self.manifest['parts']:
            session = ort.InferenceSession(str(cache / part['path']), sess_options=options,
                providers=[('CUDAExecutionProvider', {
                    'user_compute_stream': str(torch.cuda.current_stream().cuda_stream),
                    'do_copy_in_default_stream': '1'}), 'CPUExecutionProvider'])
            if session.get_providers()[0] != 'CUDAExecutionProvider':
                raise RuntimeError('portrait_warp_cuda_unavailable')
            buffers = {}
            for value in session.get_outputs():
                if value.type != ('tensor(float16)' if half else 'tensor(float)') or any(type(n) is not int or n <= 0 for n in value.shape):
                    raise RuntimeError('unsupported_portrait_warp_shape')
                buffers[value.name] = torch.empty(value.shape, dtype=self.dtype, device='cuda')
            self.sessions.append(session)
            self.buffers.append(buffers)

    def predict(self, *data):
        import numpy as np
        torch = self.torch
        np_dtype = np.float16 if self.dtype == torch.float16 else np.float32
        tensors = {name: torch.as_tensor(value, dtype=self.dtype, device='cuda').contiguous()
                   for name, value in zip(self.manifest['inputs'], data)}
        with torch.inference_mode():
            for index, session in enumerate(self.sessions):
                binding = session.io_binding()
                for value in session.get_inputs():
                    tensor = tensors[value.name]
                    binding.bind_input(value.name, 'cuda', 0, np_dtype, tuple(tensor.shape), tensor.data_ptr())
                for name, tensor in self.buffers[index].items():
                    binding.bind_output(name, 'cuda', 0, np_dtype, tuple(tensor.shape), tensor.data_ptr())
                session.run_with_iobinding(binding)
                tensors.update(self.buffers[index])
                if index < 2:
                    grid = self.manifest['grids'][index]
                    attrs = grid['attributes']
                    tensors[grid['output']] = torch.nn.functional.grid_sample(
                        tensors[grid['inputs'][0]], tensors[grid['inputs'][1]],
                        mode=attrs.get('mode', 'bilinear'), padding_mode=attrs.get('padding_mode', 'zeros'),
                        align_corners=bool(attrs.get('align_corners', 0)))
            return [tensors[self.manifest['output']].cpu().numpy()]
