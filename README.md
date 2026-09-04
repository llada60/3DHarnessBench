# 3DHarnessBench Project Page

Static project page following the section order of the SceneGen project page:

1. Hero / title / authors / links
2. TL;DR and Abstract
3. Interactive Results Gallery
4. Overview
5. Architecture
6. Results
7. Acknowledgements
8. BibTeX

## GitHub Pages

All site files live in the repository root. Configure GitHub Pages as:

**Settings → Pages → Deploy from a branch → `page` / `/(root)`**

The `.nojekyll` marker keeps this dependency-free site on the plain static-file path.

Published site: <https://llada60.github.io/3DHarnessBench/>

## Public links

Edit `config.js`:

```js
window.PROJECT_LINKS = {
  paper: "https://...",
  code: "https://...",
  data: "https://..."
};
```

The page has no external runtime dependencies.
