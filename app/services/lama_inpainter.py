"""LaMa inpainting via ONNX Runtime — leve (CPU, threads limitadas, sem torch).

Pré/pós-processamento idêntico ao demo oficial do Carve/LaMa-ONNX:
redimensiona para 512x512 (o modelo exportado é de resolução FIXA),
imagem CHW float32 /255, máscara binária 0/1, pad múltiplo de 8
(symmetric), saída já vem em [0,255] (o ONNX multiplica por 255).
"""
import threading

import numpy as np
from PIL import Image

from app.config import settings

LAMA_SIZE = 512  # resolução fixa do modelo Carve/LaMa-ONNX

_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = settings.lama_threads
                opts.inter_op_num_threads = 1
                # BASIC: otimização de grafo leve na carga (ALL é caro
                # demais para este PC — causava pico de RAM/swap).
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
                _session = ort.InferenceSession(
                    settings.lama_model_path,
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
    return _session


def _ceil_modulo(x: int, mod: int) -> int:
    return x if x % mod == 0 else (x // mod + 1) * mod


def _pad_to_modulo(arr: np.ndarray, mod: int) -> np.ndarray:
    _, h, w = arr.shape
    ph, pw = _ceil_modulo(h, mod), _ceil_modulo(w, mod)
    if ph == h and pw == w:
        return arr
    return np.pad(arr, ((0, 0), (0, ph - h), (0, pw - w)), mode="symmetric")


def inpaint_pil(image: Image.Image, mask: Image.Image) -> Image.Image:
    """image = RGB PIL; mask = grayscale PIL (branco = remover).

    Retorna RGB PIL com as mesmas dimensões de ``image`` (512x512).
    """
    img = image.convert("RGB").resize((LAMA_SIZE, LAMA_SIZE), Image.LANCZOS)
    msk = mask.convert("L").resize((LAMA_SIZE, LAMA_SIZE), Image.NEAREST)
    h = w = LAMA_SIZE
    img = np.array(img)
    msk = np.array(msk)
    img = np.transpose(img, (2, 0, 1)).astype(np.float32) / 255.0
    msk = msk[np.newaxis, ...].astype(np.float32) / 255.0

    img = _pad_to_modulo(img, 8)
    msk = _pad_to_modulo(msk, 8)
    msk = ((msk > 0) * 1.0).astype(np.float32)

    sess = _get_session()
    out = sess.run(None, {"image": img[None], "mask": msk[None]})[0][0]
    out = out[:, :h, :w]
    out = np.transpose(out, (1, 2, 0)).astype(np.uint8)
    return Image.fromarray(out, "RGB")
