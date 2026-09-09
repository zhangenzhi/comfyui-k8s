"""ComfyUI custom nodes: MiniMax-H3 video+audio generation on the HPC (SGLang).

The ComfyUI pod on Kubernetes talks HTTP directly to the SGLang server that
`minimax-h3/scripts/serve_h3.pbs` starts on a PBS GPU node. Nothing here runs
on the pod's own GPU.
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
