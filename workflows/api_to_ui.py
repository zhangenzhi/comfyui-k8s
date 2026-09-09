#!/usr/bin/env python3
"""Convert a ComfyUI API-format prompt ({id: {class_type, inputs}}) into a UI workflow
(LiteGraph JSON) that the ComfyUI sidebar can open. Needs /api/object_info for input order.
usage: api_to_ui.py object_info.json api_prompt.json out.json [--layout col1,col2,...]"""
import json
import sys

WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN"}


def is_widget(spec):
    t, opts = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
    if opts.get("forceInput"):
        return False
    return isinstance(t, list) or t in WIDGET_TYPES


def widget_typename(spec):
    return "COMBO" if isinstance(spec[0], list) else spec[0]


def default_value(spec):
    t, opts = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
    if "default" in opts:
        return opts["default"]
    if isinstance(t, list):
        return t[0] if t else ""
    return {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(t, None)


def convert(object_info, prompt, columns=None, col_w=520, row_h=40):
    ids = {k: i + 1 for i, k in enumerate(prompt.keys())}       # api id -> int node id
    nodes, links = [], []
    link_id = 0
    out_links = {}                                               # (node, slot) -> [link ids]
    pending = []                                                 # (to_node, to_slot_index, from_api, from_slot, type)
    # first pass: build nodes
    for api_id, spec in prompt.items():
        ctype = spec["class_type"]
        info = object_info[ctype]
        inputs_def = list(info["input"].get("required", {}).items()) + list(info["input"].get("optional", {}).items())
        node = {"id": ids[api_id], "type": ctype, "pos": [0, 0], "size": [420, 200], "flags": {},
                "order": ids[api_id] - 1, "mode": 0, "inputs": [], "outputs": [],
                "properties": {"Node name for S&R": ctype}, "widgets_values": []}
        for name, idef in inputs_def:
            val = spec["inputs"].get(name, None)
            linked = isinstance(val, list) and len(val) == 2 and str(val[0]) in prompt
            if is_widget(idef):
                node["widgets_values"].append(default_value(idef) if linked or val is None else val)
                opts = idef[1] if len(idef) > 1 and isinstance(idef[1], dict) else {}
                if opts.get("control_after_generate"):
                    node["widgets_values"].append("fixed")
                if linked:
                    node["inputs"].append({"name": name, "type": widget_typename(idef), "link": None,
                                           "widget": {"name": name}})
                    pending.append((ids[api_id], len(node["inputs"]) - 1, str(val[0]), val[1], widget_typename(idef)))
            else:
                node["inputs"].append({"name": name, "type": idef[0] if not isinstance(idef[0], list) else "COMBO", "link": None})
                if linked:
                    pending.append((ids[api_id], len(node["inputs"]) - 1, str(val[0]), val[1], idef[0]))
        for si, (otype, oname) in enumerate(zip(info["output"], info["output_name"])):
            node["outputs"].append({"name": oname, "type": otype, "links": [], "slot_index": si})
        n_rows = len(node["inputs"]) + len(node["widgets_values"])
        node["size"] = [420, 60 + row_h * max(3, n_rows)]
        nodes.append(node)
    by_id = {n["id"]: n for n in nodes}
    for to_node, to_slot, from_api, from_slot, ltype in pending:
        link_id += 1
        from_node = ids[from_api]
        links.append([link_id, from_node, from_slot, to_node, to_slot, ltype])
        by_id[to_node]["inputs"][to_slot]["link"] = link_id
        by_id[from_node]["outputs"][from_slot]["links"].append(link_id)
    # layout: columns of api ids
    if columns:
        for ci, col in enumerate(columns):
            y = 0
            for api_id in col:
                n = by_id[ids[api_id]]
                n["pos"] = [40 + ci * col_w, 40 + y]
                y += n["size"][1] + 40
    return {"last_node_id": len(nodes), "last_link_id": link_id, "nodes": nodes, "links": links,
            "groups": [], "config": {}, "extra": {}, "version": 0.4}


if __name__ == "__main__":
    oi = json.load(open(sys.argv[1]))
    pr = json.load(open(sys.argv[2]))
    pr = pr.get("prompt", pr)
    cols = None
    if len(sys.argv) > 4 and sys.argv[4] == "--layout":
        cols = [c.split(",") for c in sys.argv[5:]]
    json.dump(convert(oi, pr, cols), open(sys.argv[3], "w"), ensure_ascii=False, indent=1)
    print("wrote", sys.argv[3])
