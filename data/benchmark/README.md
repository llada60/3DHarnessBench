# Benchmark data

Benchmark assets are not committed because the complete dataset is several
gigabytes. Place each instance in its own directory:

```text
data/benchmark/<instance>/
├── <instance>.glb
├── <instance>_grey.glb
├── prompt.txt
├── color_renders/
│   ├── Image_005.png
│   ├── Image_015.png
│   ├── Image_025.png
│   └── Image_035.png
└── grey_renders/
    ├── Image_005.png
    ├── Image_015.png
    ├── Image_025.png
    └── Image_035.png
```

The MCP settings require both GLBs. Single-view consumes `Image_005.png` by
default; Multi-view consumes the available image files in the selected render
directory. Evaluation expects the four canonical filenames above.

Dataset licensing is separate from the repository's GPL-3.0 license. Confirm
that you have redistribution rights before publishing benchmark assets.
