# Bundled fonts

The active card renderer uses the unchanged Nimbus Sans Regular and Bold OpenType
fonts supplied with the approved design in `other/codex_schedule_handoff.zip`:
`font-regular.otf` and `font-bold.otf`. The supplied copyright and license text is
retained verbatim in `FONT_LICENSE.txt` (URW Base35, AGPL-3 with Font exception;
see that file for the full terms and notices).

Upstream source recorded in the supplied license:
[ArtifexSoftware/urw-base35-fonts](https://github.com/ArtifexSoftware/urw-base35-fonts).

The files are bundled in the Python package so offline Docker rendering uses the
same font metrics as the approved reference, without host font substitutions.

SHA-256:

- `font-regular.otf`: `7c25be4d78155523080ab85b10277150657ff7dabbcad7037bdd536c9b6d0d08`
- `font-bold.otf`: `7f33328e6b4d4cd21b45fa625791928c9407dc702db6780e56b09ca9a3ecaa67`
- `FONT_LICENSE.txt`: `1315a62758a603a3e6e87a79b8389563789c59c1740b9db8bc2b20e2e2145458`

## Previous font set

The previous Noto Sans Regular and SemiBold files remain in the package but are
not used by the active renderer. They are distributed unchanged under the SIL
Open Font License 1.1; the complete copyright and license text is in `OFL.txt`.

Source: the archived official [notofonts/noto-fonts repository](https://github.com/notofonts/noto-fonts),
`hinted/ttf/NotoSans/NotoSans-Regular.ttf` and `NotoSans-SemiBold.ttf`.

SHA-256:

- Regular: `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5`
- SemiBold: `87a8b90ece1e89746b544e4e086f85a3710e41485a8078f9be874837dfad45d5`
- OFL: `0dab92d0544f7b233403f14b84a663bdbfa746982eda629e7f4f9ffe1b036feb`
