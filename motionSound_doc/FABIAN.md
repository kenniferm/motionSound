# FABIANの導入

## データを入れる

[公式FABIAN SOFAファイル](https://sofacoustics.org/data/database/tu-berlin/FABIAN_HRIR_measured_HATO_0.sofa) を取得し、依存を導入済みのプロジェクト直下で実行します。

```sh
.venv/bin/python -m soundlab.spatial import-dataset /path/to/FABIAN_HRIR_measured_HATO_0.sofa
```

このコマンドはSOFAと48 kHzのキャッシュを保存します。アプリと別のデータディレクトリを使う場合は、CLIの `import-dataset` より前に `--data /path/to/data` を指定します。

測定データはBrinkmann et al.によるFABIAN HRIR databaseのHATO 0です。帰属とライセンスは配布資料を参照してください。 [論文](https://doi.org/10.17743/jaes.2017.0033) · [配布資料](https://sofacoustics.org/data/database/tu-berlin/FABIAN_Documentation.pdf) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
