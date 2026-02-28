# SPDX-License-Identifier: MIT
#
# MIT License
#
# Copyright (c) 2026 C-Carter
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Arnold MaterialX to UE5 (Substrate) MaterialX Converter
========================================================
Converts .mtlx files exported from Maya/Arnold (arnoldToMaterialX)
into a format compatible with UE5 Interchange MaterialX importer.

Conversions applied:
  - Remove xi:include / xmlns:xi XInclude declarations
  - Normalize Arnold node tag names to MaterialX standard (ARNOLD_NODE_TAG_MAP)
  - Normalize Arnold input names to MaterialX standard (ARNOLD_INPUT_MAP)
  - standard_surface type="closure" -> type="surfaceshader"
  - Rename nodegraph names and node names: replace '/' and '.' with '_'
  - image node:
      filename input  -> file input (type="filename")
      color_space input -> colorspace attribute
      type color4 -> color3 (or float/vector3 depending on usage)
      Remove Arnold-only inputs (ignore_missing_textures, etc.)
  - uv_transform -> texcoord + multiply (repeat value passed as UV scale)
      Note: Maya repeat=N means tile N times, so UV coords are multiplied by N
  - color_correct -> expanded into standard nodes:
      gamma      -> power(in1, in2=1/gamma)
      saturation -> mix(luminance(in), in, saturation)
      contrast   -> (in - 0.5) * contrast + 0.5
      gain       -> multiply
      lift       -> add
      hue_shift  -> omitted (no standard hue-rotation node in UE5)
  - channels="r" output -> image type="float" with direct connection
  - normal_map -> normalmap (no underscore), input "input" -> "in"
      image type -> vector3 for normal inputs
  - Type mismatch fixes (color4 -> color3 via convert node)
  - Unsupported nodes -> passthrough (rewire to first input's nodename)
      Nodes with no nodename input -> replaced with constant(0)
      Chains of unsupported nodes are resolved iteratively

Usage:
  python arnold_mtlx_to_ue5.py input.mtlx output.mtlx

Requirements: Python standard library only (xml.etree.ElementTree)
"""

import xml.etree.ElementTree as ET
import re, sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def safe_name(n):
    """Replace '/' and '.' with '_' to produce valid XML identifiers."""
    return re.sub(r'[/\.]', '_', n)

def find_input(node, name):
    for i in node.findall("input"):
        if i.get("name") == name:
            return i
    return None

def get_float_input(node, name, default=0.0):
    i = find_input(node, name)
    if i is not None:
        try:
            return float(i.get("value", default))
        except (TypeError, ValueError):
            pass
    return default

# Arnold-specific inputs that have no MaterialX equivalent
ARNOLD_ONLY_INPUTS = {
    "ignore_missing_textures",
    "missing_texture_color",
    "alpha_is_luminance",
}

# Map Arnold colorspace strings to MaterialX colorspace names
COLORSPACE_MAP = {
    "sRGB":                        "srgb_texture",
    "sRGB Encoded Rec.709 (sRGB)": "srgb_texture",
    "Raw":                         "lin_rec709",
    "raw":                         "lin_rec709",
    "Linear Rec.709 (sRGB)":       "lin_rec709",
    "Linear":                      "lin_rec709",
    "ACEScg":                      "acescg",
    "ACES2065-1":                  "acescg",
    "scene-linear Rec.709-sRGB":   "lin_rec709",
}

# Whitelist of node tags supported by UE5 MaterialX Interchange.
# Any node NOT in this set will be passthrough-replaced.
UE5_SUPPORTED_NODES = {
    # Shaders
    "standard_surface", "surfacematerial",
    # Texture
    "image", "tiledimage",
    # Math: arithmetic
    "add", "subtract", "multiply", "divide",
    "power", "sqrt", "absval", "floor", "ceil", "round",
    "sin", "cos", "tan", "min", "max", "clamp", "modulo",
    "sign", "fract",
    # Math: vector
    "dotproduct", "crossproduct", "normalize", "magnitude",
    "rotate2d", "rotate3d",
    # Math: logical
    "ifgreater", "ifgreatereq", "ifequal",
    # Compositing
    "mix", "blend", "inside", "outside", "screen",
    "disjointover", "in", "mask", "matte", "out", "over",
    "plus", "premult", "unpremult",
    # Conversion
    "convert", "extract",
    "remap", "smoothstep", "curveadjust",
    # Color
    "luminance", "rgbtohsv", "hsvtorgb",
    # UV
    "texcoord", "place2d",
    # Normal
    "normalmap", "normal",
    # Constants / gradients
    "constant", "ramplr", "ramptb",
    "splitlr", "splittb",
    # Geometry
    "position", "tangent", "bitangent",
    "geomcolor", "geompropvalue",
}

# Map Arnold node tag names -> MaterialX standard tag names.
# Nodes that require parameter expansion (e.g. color_correct) are mapped
# to their intermediate name and handled separately.
ARNOLD_NODE_TAG_MAP = {
    "aiColorCorrect": "color_correct",  # expanded by expand_color_corrects()
    "aiNormalMap":    "normal_map",     # converted to normalmap by fix_type_mismatches()
    "aiMultiply":     "multiply",
    "aiAdd":          "add",
    "aiSubtract":     "subtract",
    "aiDivide":       "divide",
    "aiAbs":          "absval",
    "aiSqrt":         "sqrt",
    "aiPow":          "power",
    "aiDot":          "dotproduct",
    "aiCross":        "crossproduct",
    "aiNormalize":    "normalize",
    "aiLength":       "magnitude",
    "aiMixShader":    "mix",
    "aiMix":          "mix",
    "aiLerp":         "mix",
    "aiClamp":        "clamp",
    "aiMin":          "min",
    "aiMax":          "max",
    "aiFloor":        "floor",
    "aiCeil":         "ceil",
    "aiRound":        "round",
    "aiModulo":       "modulo",
    "aiSin":          "sin",
    "aiCos":          "cos",
    "aiTan":          "tan",
}

# Map Arnold input names -> MaterialX standard input names per node type.
ARNOLD_INPUT_MAP = {
    "multiply":   {"input1": "in1", "input2": "in2"},
    "add":        {"input1": "in1", "input2": "in2"},
    "subtract":   {"input1": "in1", "input2": "in2"},
    "divide":     {"input1": "in1", "input2": "in2"},
    "mix":        {"input1": "fg",  "input2": "bg", "weight": "mix"},
    "clamp":      {"input": "in"},
    "min":        {"input1": "in1", "input2": "in2"},
    "max":        {"input1": "in1", "input2": "in2"},
    "dotproduct": {"input1": "in1", "input2": "in2"},
    "power":      {"input": "in1", "exponent": "in2"},
    "absval":     {"input": "in"},
    "sqrt":       {"input": "in"},
    "floor":      {"input": "in"},
    "ceil":       {"input": "in"},
    "round":      {"input": "in"},
    "normalize":  {"input": "in"},
    "magnitude":  {"input": "in"},
    "sin":        {"input": "in"},
    "cos":        {"input": "in"},
    "tan":        {"input": "in"},
    "modulo":     {"input1": "in1", "input2": "in2"},
    "normal_map": {"input": "in"},
}

# ---------------------------------------------------------------------------
# Step 1: Remove XInclude declarations and include elements
# ---------------------------------------------------------------------------

def strip_includes_and_ns(root):
    """Remove xmlns:xi / xmlns:ns0 attributes and xi:include elements."""
    for attr in list(root.attrib.keys()):
        if "xi" in attr or "XInclude" in attr or "ns0" in attr:
            del root.attrib[attr]
    to_remove = [c for c in root if "include" in c.tag.lower()]
    for el in to_remove:
        root.remove(el)

# ---------------------------------------------------------------------------
# Step 2a: Normalize Arnold node tags and input names
# ---------------------------------------------------------------------------

def normalize_arnold_nodes(root):
    """
    Walk all nodegraphs and:
    1. Rename Arnold node tags to MaterialX standard names (ARNOLD_NODE_TAG_MAP)
    2. Rename Arnold input names to MaterialX standard names (ARNOLD_INPUT_MAP)
    """
    for ng in root.findall("nodegraph"):
        for node in ng:
            tag = node.tag
            if tag in ARNOLD_NODE_TAG_MAP:
                node.tag = ARNOLD_NODE_TAG_MAP[tag]
                tag = node.tag
            if tag in ARNOLD_INPUT_MAP:
                imap = ARNOLD_INPUT_MAP[tag]
                for inp in node.findall("input"):
                    old_name = inp.get("name", "")
                    if old_name in imap:
                        inp.set("name", imap[old_name])

        # Expand aiNegate -> multiply(in1=<src>, in2=-1)
        for node in list(ng.findall("aiNegate")):
            node.tag = "multiply"
            node_type = node.get("type", "color3")
            inp = find_input(node, "input")
            if inp is not None:
                inp.set("name", "in1")
            in2 = ET.SubElement(node, "input")
            in2.set("name", "in2")
            in2.set("type", "float")
            in2.set("value", "-1")
            print(f"  [aiNegate] expanded to multiply(in, -1): {node.get('name')}")

        # Expand aiFraction -> subtract(x, floor(x))  i.e. frac(x) = x - floor(x)
        for node in list(ng.findall("aiFraction")):
            node_name = node.get("name", "")
            node_type = node.get("type", "color3")
            src_inp = find_input(node, "input")
            src_name = src_inp.get("nodename", "") if src_inp is not None else ""
            src_out  = src_inp.get("output",   "") if src_inp is not None else ""

            # If input is value-only, insert a constant node first
            src_const = None
            if not src_name and src_inp is not None:
                src_val = src_inp.get("value", "0")
                src_const = f"const_frac_{node_name}"
                cn = ET.SubElement(ng, "constant"); cn.set("name", src_const); cn.set("type", node_type)
                vi = ET.SubElement(cn, "input"); vi.set("name", "value"); vi.set("type", node_type); vi.set("value", src_val)
                src_name = src_const

            # insert floor node before subtract
            floor_name = f"floor_{node_name}"
            floor_node = ET.SubElement(ng, "floor")
            floor_node.set("name", floor_name)
            floor_node.set("type", node_type)
            fi = ET.SubElement(floor_node, "input")
            fi.set("name", "in"); fi.set("type", node_type)
            if src_name: fi.set("nodename", src_name)
            if src_out:  fi.set("output", src_out)

            # convert aiFraction to subtract(in1=src, in2=floor)
            node.tag = "subtract"
            for old_inp in list(node.findall("input")):
                node.remove(old_inp)
            in1 = ET.SubElement(node, "input")
            in1.set("name", "in1"); in1.set("type", node_type)
            if src_name: in1.set("nodename", src_name)
            if src_out:  in1.set("output", src_out)
            in2 = ET.SubElement(node, "input")
            in2.set("name", "in2"); in2.set("type", node_type)
            in2.set("nodename", floor_name)
            print(f"  [aiFraction] expanded to subtract(x, floor(x)): {node_name}")

# ---------------------------------------------------------------------------
# Step 2b: Fix standard_surface type
# ---------------------------------------------------------------------------

def fix_standard_surface(root):
    """Change standard_surface type='closure' to type='surfaceshader'."""
    for n in root.findall("standard_surface"):
        if n.get("type") == "closure":
            n.set("type", "surfaceshader")

# ---------------------------------------------------------------------------
# Step 3: Fix image nodes
# ---------------------------------------------------------------------------

def fix_image_node(node, target_type="color3"):
    """
    Fix an image node for UE5 compatibility:
    - Set type to target_type
    - Convert color_space input to colorspace attribute
    - Rename 'filename' input to 'file' with type='filename'
    - Remove Arnold-only inputs
    """
    node.set("type", target_type)

    cs = find_input(node, "color_space")
    if cs is not None:
        node.set("colorspace", COLORSPACE_MAP.get(cs.get("value", ""), cs.get("value", "")))
        node.remove(cs)

    fn = find_input(node, "filename")
    if fn is not None:
        fn.set("name", "file")
        fn.set("type", "filename")
    f = find_input(node, "file")
    if f is not None:
        f.set("type", "filename")

    for inp in list(node.findall("input")):
        if inp.get("name") in ARNOLD_ONLY_INPUTS:
            node.remove(inp)

# ---------------------------------------------------------------------------
# Step 4: Rename nodegraph and node names (replace '/' and '.' with '_')
# ---------------------------------------------------------------------------

def rename_nodegraphs(root):
    """Rename nodegraph names and update all references in standard_surface inputs."""
    rmap = {}
    for ng in root.findall("nodegraph"):
        old = ng.get("name", "")
        new = safe_name(old)
        if new != old:
            rmap[old] = new
            ng.set("name", new)
    for node in root:
        for inp in node.findall("input"):
            ref = inp.get("nodegraph", "")
            if ref in rmap:
                inp.set("nodegraph", rmap[ref])
            out = inp.get("output", "")
            s = safe_name(out)
            if s != out:
                inp.set("output", s)
    return rmap

def rename_nodes(ng):
    """Rename all node names inside a nodegraph and update internal references."""
    rmap = {}
    for node in ng:
        old = node.get("name", "")
        new = safe_name(old)
        if new != old:
            rmap[old] = new
    for node in ng:
        if node.get("name", "") in rmap:
            node.set("name", rmap[node.get("name")])
        for inp in node.findall("input"):
            nn = inp.get("nodename", "")
            if nn in rmap:
                inp.set("nodename", rmap[nn])
    for out in ng.findall("output"):
        nn = out.get("nodename", "")
        if nn in rmap:
            out.set("nodename", rmap[nn])
    return rmap

# ---------------------------------------------------------------------------
# Step 5: Handle output channels and float outputs
# ---------------------------------------------------------------------------

def fix_output_channels(ng):
    """
    Normalize output names (replace '/' and '.' with '_').
    Remove channels attribute (float image conversion is done later
    by fix_float_outputs after uv_transform expansion).
    """
    for out in list(ng.findall("output")):
        out_name = out.get("name", "")
        new_name = safe_name(out_name)
        if new_name != out_name:
            out.set("name", new_name)
        out.attrib.pop("channels", None)


def fix_float_outputs(ng):
    """
    For outputs with type='float', trace back to the source image node
    and set its type to 'float', then point the output directly to the image.
    Must be called after uv_transform expansion.
    """
    for out in ng.findall("output"):
        if out.get("type", "") != "float":
            continue
        src_name = out.get("nodename", "")
        img = _trace_to_image(ng, src_name)
        if img is not None:
            img.set("type", "float")
            if not img.get("colorspace"):
                img.set("colorspace", "lin_rec709")
            out.set("nodename", img.get("name", src_name))


def _trace_to_image(ng, node_name, depth=0):
    """Recursively trace from node_name back to an image node (max depth 5)."""
    if depth > 5:
        return None
    node = _find_node(ng, node_name)
    if node is None:
        return None
    if node.tag == "image":
        return node
    if node.tag in ("place2d", "multiply"):
        # Look for an image node that has this node as its texcoord input
        for n in ng:
            if n.tag == "image":
                tc = find_input(n, "texcoord")
                if tc is not None and tc.get("nodename", "") == node_name:
                    return n
        return None
    for attr in ("passthrough", "input", "in"):
        inp = find_input(node, attr)
        if inp is not None and inp.get("nodename"):
            result = _trace_to_image(ng, inp.get("nodename"), depth + 1)
            if result is not None:
                return result
    return None

def _find_node(ng, name):
    for node in ng:
        if node.get("name") == name:
            return node
    return None

# ---------------------------------------------------------------------------
# Step 6: Expand uv_transform -> texcoord + multiply
# ---------------------------------------------------------------------------

def expand_uv_transforms(ng, uid):
    for uv in list(ng.findall("uv_transform")):
        _expand_one_uv(ng, uv, uid)

def _expand_one_uv(ng, uv, uid):
    """
    Replace a uv_transform node with texcoord + multiply.
    Maya repeat=N means tile the texture N times, which equals
    multiplying UV coordinates by N (not place2d scale which inverts the direction).
    """
    uv_name     = uv.get("name")
    passthrough = find_input(uv, "passthrough")
    repeat_inp  = find_input(uv, "repeat")
    repeat_val  = repeat_inp.get("value", "1, 1") if repeat_inp is not None else "1, 1"

    offset_inp = find_input(uv, "offset")
    offset_val = offset_inp.get("value", "0, 0") if offset_inp is not None else "0, 0"
    has_offset = offset_val not in ("0, 0", "0,0", "0 0")

    tc_name = f"texcoord_{uid[0]}"; uid[0] += 1
    tc = ET.SubElement(ng, "texcoord")
    tc.set("name", tc_name); tc.set("type", "vector2")

    mul_name = f"uv_multiply_{uid[0]}"; uid[0] += 1
    mul = ET.SubElement(ng, "multiply")
    mul.set("name", mul_name); mul.set("type", "vector2")
    mi = ET.SubElement(mul, "input"); mi.set("name", "in1"); mi.set("type", "vector2"); mi.set("nodename", tc_name)
    ms = ET.SubElement(mul, "input"); ms.set("name", "in2"); ms.set("type", "vector2"); ms.set("value", repeat_val)

    # If offset is non-zero, add an add node after multiply: UV_final = UV * repeat + offset
    last_uv_node = mul_name
    if has_offset:
        add_name = f"uv_offset_{uid[0]}"; uid[0] += 1
        add_node = ET.SubElement(ng, "add")
        add_node.set("name", add_name); add_node.set("type", "vector2")
        ai1 = ET.SubElement(add_node, "input"); ai1.set("name", "in1"); ai1.set("type", "vector2"); ai1.set("nodename", mul_name)
        ai2 = ET.SubElement(add_node, "input"); ai2.set("name", "in2"); ai2.set("type", "vector2"); ai2.set("value", offset_val)
        last_uv_node = add_name
        print(f"  [uv_transform] offset={offset_val} -> add node: {add_name}")

    # Attach texcoord to the passthrough image node
    if passthrough is not None:
        src = passthrough.get("nodename", "")
        if src:
            _attach_texcoord_to_image(ng, safe_name(src), last_uv_node)

    # Rewire downstream nodes that referenced this uv_transform
    # to point directly to the image node (which now has texcoord attached)
    uv_safe  = safe_name(uv_name)
    img_name = safe_name(passthrough.get("nodename", "")) if passthrough is not None else ""
    for node in ng:
        for inp in node.findall("input"):
            nn = inp.get("nodename", "")
            if nn == uv_name or nn == uv_safe:
                if img_name:
                    inp.set("nodename", img_name)
                    inp.attrib.pop("type", None)  # type will be repaired later

    # Rewire output references
    passthrough_src = safe_name(passthrough.get("nodename", "")) if passthrough is not None else ""
    for out in ng.findall("output"):
        nn = out.get("nodename", "")
        if nn == uv_name or nn == uv_safe:
            if passthrough_src:
                out.set("nodename", passthrough_src)

    ng.remove(uv)

def _attach_texcoord_to_image(ng, node_name, mul_name):
    """Recursively find the image node and attach the UV multiply node as texcoord."""
    node = _find_node(ng, node_name)
    if node is None:
        return
    if node.tag == "image":
        tc = find_input(node, "texcoord")
        if tc is None:
            tc = ET.SubElement(node, "input")
            tc.set("name", "texcoord"); tc.set("type", "vector2")
        tc.set("nodename", mul_name)
    else:
        for attr in ("input", "in", "passthrough"):
            inp = find_input(node, attr)
            if inp is not None and inp.get("nodename"):
                _attach_texcoord_to_image(ng, safe_name(inp.get("nodename")), mul_name)
                break

# ---------------------------------------------------------------------------
# Step 7: Expand color_correct into standard nodes
# ---------------------------------------------------------------------------

def expand_color_corrects(ng, uid):
    for cc in list(ng.findall("color_correct")):
        _expand_one_cc(ng, cc, uid)

def _expand_one_cc(ng, cc, uid):
    """
    Expand color_correct into UE5-compatible standard nodes:
      gamma      -> power(in1, in2=1/gamma)
      saturation -> mix(bg=luminance(in), fg=in, mix=saturation)
      hue_shift  -> omitted (no hue-rotation node available in UE5 standard library)
      contrast   -> (in - 0.5) * contrast + 0.5
      gain       -> multiply(in1, gain)
      lift       -> add(in1, lift)
    """
    cc_name = cc.get("name")
    hue     = get_float_input(cc, "hue_shift",  0.0)  # noqa: F841 (intentionally unused)
    sat     = get_float_input(cc, "saturation", 1.0)
    con     = get_float_input(cc, "contrast",   1.0)
    gain    = get_float_input(cc, "gain",       1.0)
    gamma_v = get_float_input(cc, "gamma",      1.0)
    lift    = get_float_input(cc, "lift",       0.0)

    src_inp   = find_input(cc, "input")
    last_name = src_inp.get("nodename") if src_inp is not None else None
    last_out  = src_inp.get("output")   if src_inp is not None else None
    wt = "color3"

    # If input is value-only (no nodename), create a constant node as the source
    if src_inp is not None and not last_name:
        val = src_inp.get("value", "0, 0, 0")
        const_name = f"const_{cc.get('name', 'cc')}_{uid[0]}"; uid[0] += 1
        cn = ET.SubElement(ng, "constant"); cn.set("name", const_name); cn.set("type", wt)
        vi = ET.SubElement(cn, "input"); vi.set("name", "value"); vi.set("type", wt); vi.set("value", val)
        last_name = const_name
        print(f"  [color_correct] value-only input -> constant node: {const_name} = {val}")

    def append_node(tag, in_name, extras):
        """Append a node connected to last_name and update last_name."""
        nonlocal last_name, last_out
        nn = f"{tag}_{uid[0]}"; uid[0] += 1
        n = ET.SubElement(ng, tag); n.set("name", nn); n.set("type", wt)
        i = ET.SubElement(n, "input"); i.set("name", in_name); i.set("type", wt)
        if last_name is not None: i.set("nodename", last_name)
        if last_out  is not None: i.set("output",   last_out)
        for k, t, v in extras:
            e = ET.SubElement(n, "input"); e.set("name", k); e.set("type", t); e.set("value", v)
        last_name = nn; last_out = None
        return nn

    # gamma -> power(in1, in2=1/gamma)
    if abs(gamma_v - 1.0) > 1e-4 and gamma_v > 1e-6:
        append_node("power", "in1", [("in2", "float", str(1.0 / gamma_v))])

    # saturation -> mix(bg=luminance(in), fg=in, mix=saturation)
    if abs(sat - 1.0) > 1e-4:
        lum_n = f"luminance_{uid[0]}"; uid[0] += 1
        lum = ET.SubElement(ng, "luminance"); lum.set("name", lum_n); lum.set("type", wt)
        li = ET.SubElement(lum, "input"); li.set("name", "in"); li.set("type", wt)
        if last_name: li.set("nodename", last_name)
        if last_out:  li.set("output",   last_out)

        mix_n = f"mix_{uid[0]}"; uid[0] += 1
        mx = ET.SubElement(ng, "mix"); mx.set("name", mix_n); mx.set("type", wt)
        bg_i = ET.SubElement(mx, "input"); bg_i.set("name", "bg"); bg_i.set("type", wt); bg_i.set("nodename", lum_n)
        fg_i = ET.SubElement(mx, "input"); fg_i.set("name", "fg"); fg_i.set("type", wt)
        if last_name: fg_i.set("nodename", last_name)
        if last_out:  fg_i.set("output",   last_out)
        mi_i = ET.SubElement(mx, "input"); mi_i.set("name", "mix"); mi_i.set("type", "float"); mi_i.set("value", str(sat))
        last_name = mix_n; last_out = None

    # contrast -> (in - 0.5) * contrast + 0.5
    if abs(con - 1.0) > 1e-4:
        s_n = f"sub_{uid[0]}"; uid[0] += 1
        ns = ET.SubElement(ng, "subtract"); ns.set("name", s_n); ns.set("type", wt)
        si = ET.SubElement(ns, "input"); si.set("name", "in1"); si.set("type", wt)
        if last_name: si.set("nodename", last_name)
        if last_out:  si.set("output",   last_out)
        ns2 = ET.SubElement(ns, "input"); ns2.set("name", "in2"); ns2.set("type", "float"); ns2.set("value", "0.5")

        m_n = f"multiply_{uid[0]}"; uid[0] += 1
        nm = ET.SubElement(ng, "multiply"); nm.set("name", m_n); nm.set("type", wt)
        nm1 = ET.SubElement(nm, "input"); nm1.set("name", "in1"); nm1.set("type", wt); nm1.set("nodename", s_n)
        nm2 = ET.SubElement(nm, "input"); nm2.set("name", "in2"); nm2.set("type", "float"); nm2.set("value", str(con))

        a_n = f"add_{uid[0]}"; uid[0] += 1
        na = ET.SubElement(ng, "add"); na.set("name", a_n); na.set("type", wt)
        na1 = ET.SubElement(na, "input"); na1.set("name", "in1"); na1.set("type", wt); na1.set("nodename", m_n)
        na2 = ET.SubElement(na, "input"); na2.set("name", "in2"); na2.set("type", "float"); na2.set("value", "0.5")
        last_name = a_n; last_out = None

    # gain -> multiply
    if abs(gain - 1.0) > 1e-4:
        append_node("multiply", "in1", [("in2", "float", str(gain))])

    # lift -> add
    if abs(lift) > 1e-4:
        append_node("add", "in1", [("in2", "float", str(lift))])

    # Rewire downstream references from cc_name to last_name
    cc_safe = safe_name(cc_name)
    for node in ng:
        for inp in node.findall("input"):
            if inp.get("nodename") in (cc_name, cc_safe):
                inp.set("nodename", last_name)
    for out in ng.findall("output"):
        if out.get("nodename") in (cc_name, cc_safe):
            out.set("nodename", last_name)

    ng.remove(cc)

# ---------------------------------------------------------------------------
# Step 8: Passthrough unsupported nodes
# ---------------------------------------------------------------------------

def passthrough_unsupported_nodes(ng):
    """
    Replace nodes not in UE5_SUPPORTED_NODES with a passthrough connection.
    - If the node has a nodename input: rewire all downstream references to
      that input's nodename, then remove the node.
    - If the node has no nodename input (value-only): replace with constant(0).
    Iterates until no more unsupported nodes remain (handles chains).
    """
    changed = True
    while changed:
        changed = False
        for node in list(ng):
            tag       = node.tag
            node_name = node.get("name", "")

            if tag in ("output", "look", "materialassign"):
                continue
            if tag in UE5_SUPPORTED_NODES:
                continue

            # Find the first input with a nodename (passthrough source)
            passthrough_name = None
            passthrough_type = None
            for inp in node.findall("input"):
                nn = inp.get("nodename", "")
                if nn:
                    passthrough_name = nn
                    passthrough_type = inp.get("type", "")
                    break

            node_type = node.get("type", "color3")
            node_safe = safe_name(node_name)

            if passthrough_name:
                for other in ng:
                    for inp in other.findall("input"):
                        if inp.get("nodename", "") in (node_name, node_safe):
                            inp.set("nodename", passthrough_name)
                            if passthrough_type:
                                inp.set("type", passthrough_type)
                for out in ng.findall("output"):
                    if out.get("nodename", "") in (node_name, node_safe):
                        out.set("nodename", passthrough_name)
                        if passthrough_type and out.get("type", "") != "float":
                            out.set("type", passthrough_type)
            else:
                # No nodename input: replace with constant(0)
                const_name = f"const_{node_safe}"
                const = ET.SubElement(ng, "constant")
                const.set("name", const_name)
                const.set("type", node_type)
                default_val = {
                    "color3": "0, 0, 0", "color4": "0, 0, 0, 0",
                    "float": "0", "vector2": "0, 0", "vector3": "0, 0, 0",
                }.get(node_type, "0")
                val_inp = ET.SubElement(const, "input")
                val_inp.set("name", "value"); val_inp.set("type", node_type)
                val_inp.set("value", default_val)
                for other in ng:
                    for inp in other.findall("input"):
                        if inp.get("nodename", "") in (node_name, node_safe):
                            inp.set("nodename", const_name)
                for out in ng.findall("output"):
                    if out.get("nodename", "") in (node_name, node_safe):
                        out.set("nodename", const_name)

            try:
                ng.remove(node)
                changed = True
                print(f"  [passthrough] Unsupported node '{node_name}' ({tag}) bypassed")
            except ValueError:
                pass

# ---------------------------------------------------------------------------
# Step 9: Repair missing input types
# ---------------------------------------------------------------------------

def repair_missing_input_types(ng):
    """
    Fill in missing type attributes on inputs that have a nodename reference
    but lost their type during uv_transform expansion.
    """
    for node in ng:
        node_type = node.get("type", "color3")
        for inp in node.findall("input"):
            if inp.get("type") is None and inp.get("nodename"):
                src_node = _find_node(ng, inp.get("nodename", ""))
                if src_node is not None:
                    inp.set("type", src_node.get("type", node_type))
                else:
                    inp.set("type", node_type)

# ---------------------------------------------------------------------------
# Step 10: Fix type mismatches
# ---------------------------------------------------------------------------

def fix_type_mismatches(ng, uid):
    """
    Fix remaining type mismatches:
    - normal_map -> normalmap, rename input 'input' -> 'in', set image type to vector3
    - color4 inputs to color3/float nodes -> insert convert node
    """
    for node in list(ng):
        tag = node.tag

        # Rename normal_map to normalmap (MaterialX 1.38+ standard)
        if tag == "normal_map":
            node.tag = "normalmap"
            tag = "normalmap"

        if tag == "normalmap":
            inp = find_input(node, "input")
            if inp is not None:
                inp.set("name", "in")
                inp.set("type", "vector3")
            inp = find_input(node, "in")
            if inp is not None:
                inp.set("type", "vector3")
                src_node = _find_node(ng, inp.get("nodename", ""))
                if src_node is not None and src_node.tag == "image":
                    src_node.set("type", "vector3")

        elif tag in ("multiply", "add", "subtract", "divide", "mix",
                     "min", "max", "clamp", "power", "modulo"):
            node_type = node.get("type", "color3")
            for inp_name in ("in1", "in2", "fg", "bg", "mix", "in"):
                inp = find_input(node, inp_name)
                if inp is None:
                    continue
                src = inp.get("nodename", "")
                if not src:
                    continue
                src_node = _find_node(ng, src)
                if src_node is None:
                    continue
                st = src_node.get("type", "")
                if st == "color4" and node_type in ("color3", "float"):
                    conv_name = f"convert_{uid[0]}"; uid[0] += 1
                    conv = ET.SubElement(ng, "convert")
                    conv.set("name", conv_name); conv.set("type", node_type)
                    ci = ET.SubElement(conv, "input")
                    ci.set("name", "in"); ci.set("type", "color4"); ci.set("nodename", src)
                    inp.set("nodename", conv_name)

# ---------------------------------------------------------------------------
# Step 11: Fix output types to match standard_surface input types
# ---------------------------------------------------------------------------

def fix_output_types(root):
    """
    Ensure nodegraph output types match the expected types
    declared in standard_surface inputs.
    """
    for ss in root.findall("standard_surface"):
        for inp in ss.findall("input"):
            ng_name  = inp.get("nodegraph", "")
            out_name = inp.get("output", "")
            inp_type = inp.get("type", "")
            if ng_name and out_name and inp_type:
                ng = root.find(f"nodegraph[@name='{ng_name}']")
                if ng is not None:
                    out_el = ng.find(f"output[@name='{out_name}']")
                    if out_el is not None and out_el.get("type", "") != inp_type:
                        out_el.set("type", inp_type)

# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(input_path, output_path):
    tree = ET.parse(input_path)
    root = tree.getroot()
    uid = [0]

    # 1. Remove XInclude declarations
    strip_includes_and_ns(root)

    # 2a. Normalize Arnold node tags and input names
    normalize_arnold_nodes(root)

    # 2b. Fix standard_surface type
    fix_standard_surface(root)

    # 3. Rename nodegraph names (replace '/' and '.' with '_')
    rename_nodegraphs(root)

    # 4. Process each nodegraph
    for ng in root.findall("nodegraph"):

        # 4a. Normalize output names and strip channels attribute
        fix_output_channels(ng)

        # 4b. Rename node names (replace '/' and '.' with '_')
        rename_nodes(ng)

        # 4c. Fix image nodes (type, colorspace, filename->file)
        for img in ng.findall("image"):
            current_type = img.get("type", "")
            target = current_type if current_type in ("float", "vector3") else "color3"
            fix_image_node(img, target)

        # 4d. Expand color_correct into standard nodes
        expand_color_corrects(ng, uid)

        # 4e. Expand uv_transform into texcoord + multiply
        expand_uv_transforms(ng, uid)

        # 4f. Convert float outputs to float image (must run after uv_transform expansion)
        fix_float_outputs(ng)

        # 4g. Repair input types lost during uv_transform expansion
        repair_missing_input_types(ng)

        # 4h. Fix type mismatches (normalmap, color4->color3, etc.)
        fix_type_mismatches(ng, uid)

        # 4i. Passthrough unsupported nodes to keep the network intact
        passthrough_unsupported_nodes(ng)

    # 5. Align nodegraph output types with standard_surface input types
    fix_output_types(root)

    # 6. Write output (no namespace prefix)
    _indent(root)
    xml_str = ET.tostring(root, encoding="unicode")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("<?xml version='1.0' encoding='utf-8'?>\n")
        f.write(xml_str)
    print(f"Done: {output_path}")

def _indent(elem, level=0):
    pad = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = pad
        for child in elem:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = pad

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python arnold_mtlx_to_ue5.py input.mtlx output.mtlx")
        sys.exit(1)
    if not Path(sys.argv[1]).exists():
        print(f"Error: file not found: {sys.argv[1]}")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
