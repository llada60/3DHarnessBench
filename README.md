# 3DHarnessBench Project Page

## Updating the leaderboard

The main leaderboard is generated from `assets/data/main_results.csv`. Add or edit rows there while preserving the column names, then publish the site. The leaderboard, result counts, best Full 3D Interaction Uni3D score, and interactive Uni3D chart will use the updated data automatically.

## Publishing CSS or JavaScript changes

When changing `style.css` or `script.js`, update its `?v=` value in `index.html` to the first 12 characters of the file's SHA-1 (`shasum style.css script.js`). This makes browsers request the matching assets instead of reusing an older cached navigation layout.
