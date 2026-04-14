# backward-compat shim — import from codeclub.compress.tree
from codeclub.compress.tree import *  # noqa: F401, F403
from codeclub.compress.tree import (  # noqa: F401
    _detect_language, _get_ts_parser,
    _collect_python_stubs, _walk_python,
    _collect_js_stubs, _walk_js,
    _js_node_name, _js_body_node,
    _extract_python_docstring, _build_source_map,
    _hash_node_source,
)
