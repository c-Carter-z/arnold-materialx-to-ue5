# arnold-materialx-to-ue5

A Python script that converts **MaterialX files exported from Maya/Arnold** (`arnoldToMaterialX`) into a format compatible with the **Unreal Engine 5 Interchange MaterialX importer** (Substrate).

## Background

Maya's `arnoldToMaterialX` exporter produces `.mtlx` files that contain Arnold-specific node names, non-standard attribute formats, and constructs that UE5's MaterialX importer does not recognize. This tool bridges that gap automatically.

To our knowledge, **no public tool existed** to solve this conversion problem before this script was created. Community threads on both Autodesk and Epic forums report failures with no resolution.

## Requirements

- Python 3.8+
- No third-party libraries required (standard library only)
- Maya 2024+ with Arnold (`arnoldToMaterialX` command)
- Unreal Engine 5.3+ with **Interchange** and **Substrate** enabled

## Usage

```bash
python arnold_mtlx_to_ue5.py input.mtlx output.mtlx
```

Then drag and drop `output.mtlx` into the UE5 Content Browser.

## What Gets Converted

### Structural fixes
| Issue | Fix |
|---|---|
| `xmlns:xi` / `xi:include` declarations | Removed |
| `standard_surface type="closure"` | Changed to `type="surfaceshader"` |
| Node/nodegraph names containing `/` or `.` | Replaced with `_` |

### Arnold node normalization
| Arnold node | MaterialX standard |
|---|---|
| `aiColorCorrect` | `color_correct` → expanded (see below) |
| `aiNormalMap` | `normalmap` |
| `aiMultiply` | `multiply` |
| `aiAdd` | `add` |
| `aiMix` / `aiLerp` | `mix` |
| `aiClamp` | `clamp` |
| `aiPow` | `power` |
| ... and more | (see `ARNOLD_NODE_TAG_MAP` in script) |

### color_correct expansion
`color_correct` is expanded into UE5-compatible standard nodes:

| Parameter | Expanded to |
|---|---|
| `gamma` | `power(in, 1/gamma)` |
| `saturation` | `mix(luminance(in), in, saturation)` |
| `contrast` | `(in - 0.5) * contrast + 0.5` |
| `gain` | `multiply(in, gain)` |
| `lift` | `add(in, lift)` |
| `hue_shift` | ⚠ Omitted (no hue-rotation node in UE5 standard library) |

### uv_transform expansion
`uv_transform` is replaced with `texcoord + multiply`:

```
texcoord → multiply(in2 = repeat_value) → image.texcoord
```

> **Note:** Maya's `repeat=2` means "tile the texture 2 times", which equals multiplying UV coordinates by 2. UE5's `place2d` scale works in the opposite direction, so `multiply` is used instead.

### image node fixes
| Issue | Fix |
|---|---|
| `filename` input name | Renamed to `file` |
| `filename` input type `string` | Changed to `filename` |
| `color_space` input | Converted to `colorspace` attribute |
| `type="color4"` | Changed to `color3` (or `float` / `vector3` as needed) |
| Arnold-only inputs | Removed (`ignore_missing_textures`, `missing_texture_color`, `alpha_is_luminance`) |
| Grayscale channels (`channels="r"`) | Image type set to `float`, direct connection |
| Normal map images | Image type set to `vector3` |

### Colorspace mapping
| Arnold value | MaterialX value |
|---|---|
| `sRGB` / `sRGB Encoded Rec.709 (sRGB)` | `srgb_texture` |
| `Raw` / `Linear` / `Linear Rec.709 (sRGB)` | `lin_rec709` |
| `ACEScg` / `ACES2065-1` | `acescg` |

### Unsupported node passthrough
Any node not in UE5's supported node set is automatically **bypassed**:
- Its downstream connections are rewired to its first input's source.
- If it has no nodename input, it is replaced with `constant(0)`.
- Chains of unsupported nodes are resolved iteratively.

This ensures the material network is never broken by an unknown node, though the visual result may differ from the original.

## Known Limitations

- `hue_shift` in `color_correct` is not converted (no equivalent in UE5 standard nodes)
- `uv_transform` only converts `repeat` (UV tiling); rotation and offset are ignored
- Unsupported nodes are bypassed with their primary input — visual fidelity is not guaranteed
- Absolute texture paths (e.g. `Z:/project/...`) are preserved as-is; you may need to relocate textures

## Tested With

- Maya 2026 + Arnold 7.x (`arnoldToMaterialX`)
- Unreal Engine 5.5 (Substrate + Interchange MaterialX importer)
- MaterialX version 1.39

## License

MIT License — see [LICENSE](LICENSE)

## Contributing

Bug reports and pull requests are welcome. If you encounter a node type that is not yet handled, please open an issue with the relevant `.mtlx` snippet.
