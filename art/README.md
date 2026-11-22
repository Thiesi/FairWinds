# Artwork

| File | Size | Use |
| --- | --- | --- |
| `banner.png` | 1280 x 640 | README hero and GitHub social preview |
| `logo.png` | 1120 x 320 | Wordmark with the balloon mark, for the game's web page |
| `logo-mark.png` | 512 x 512 | The balloon mark alone |

All three are concept art, not gameplay screenshots.

## How they are made

`render_art.py` draws them as pixel art on a small canvas and scales them up
with hard edges, the way Voidrunner, War Dialer and Retro Trivia render their
banners. It needs only the standard library and always writes the same bytes:

```sh
python art/render_art.py
```

Lunar Lander and Karst 41 used an image-generation tool for their banners
instead. These were rendered by script because no such tool was available in
the session that made them. The owner may replace `banner.png` with generated
art at the same size; nothing else depends on how it was made.

Once the game has a splash scene, `render_art.py` should reuse the game's own
sky, balloon and palette, as the other doors' banner scripts do.

## Setting the social preview

GitHub does not take the social preview from the README. Open **Settings →
General → Social preview → Edit → Upload an image** and choose `banner.png`.
