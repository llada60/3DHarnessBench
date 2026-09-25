# Benchmark data

Download [3DHarnessBench from Hugging Face](https://huggingface.co/datasets/lingada/3DHarnessBench)
and place each instance in its own directory under `benchmark/`:

```text
benchmark/<instance>/
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

Dataset licensing is separate from the repository's Apache-2.0 license. Confirm
that you have redistribution rights before publishing benchmark assets.
