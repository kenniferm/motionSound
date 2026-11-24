---
{"id": "sofa-format-background", "title": "Gate 2に向けたHRTF・SOFAの前提", "sources": ["https://www.sofaconventions.org/mediawiki/index.php/SOFA_(Spatially_Oriented_Format_for_Acoustics)"], "retrieved_date": "2026-11-23", "status": "source_checked", "source_title": "SOFA (Spatially Oriented Format for Acoustics)", "source_author": "SOFA project", "source_publication_date": null, "source_page_updated": "2026-09-06"}
---
# 問い
HRTFを使う実験で、データ形式とレンダラーの役割をどう分けるか？

## 一次資料で確認したこと
SOFA公式サイトは、HRTFや空間インパルス応答を保存・交換する形式としてSOFAを説明し、AES69標準との関係を記載する。SOFA自体は音響効果の生成アルゴリズムや特定データセットの品質保証ではない。

## 実験の提案（未検証の仮説）
Gate 2で、出典・利用条件・測定座標を確認したデータセットと対応ツールを比較し、同じ乾いた音源と軌跡からステレオパンとヘッドホン向けHRTF出力を作る。聞こえた方向・高さ・距離を記録する。

## 条件と限界
HRTFデータセット、補間法、ライブラリ、ライセンスは未選定。このノートを参照してもHRTF実装済みとは言えない。Gate 1では外部データや依存を勝手に追加しない。

## 実装・観察との接続
将来の実験では乾いた音源、時刻付き軌跡、renderer設定を任意の成果物として保存する。音が頭の外に感じられるかは予想であり、人間の観察結果はまだない。試聴結果は関連候補・評価IDに追記し、原文を残す。
