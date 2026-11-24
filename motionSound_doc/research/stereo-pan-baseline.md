---
{"id": "stereo-pan-baseline", "title": "ステレオパンを比較基準にする", "sources": ["https://www.w3.org/TR/2021/REC-webaudio-20210617/#Spatialization-panning"], "retrieved_date": "2026-11-23", "status": "source_checked", "source_title": "Web Audio API, section 6.3", "source_author": "W3C Web Audio Working Group", "source_publication_date": "2021-06-17"}
---
# 問い
左右への移動を作ったことと、前後・高さを知覚できることを区別するには？

## 一次資料で確認したこと
W3C Web Audio API、2021年6月17日勧告の6.3節はequal-powerパンとHRTF方式を区別する。前者の計算では仰角を無視し、後者は方向ごとの測定インパルス応答を用いる。Web Audioの仕様であり、このPythonアプリの効果を検証した資料ではない。

## 実験の提案（未検証の仮説）
同じ素材・長さ・軌跡で単純な左右パンを比較基準にする。左右移動が聞こえるかをまず評価し、前後・高さ・頭外の定位は独立して質問する。Gate 2でHRTF方式と比較する。

## 条件と限界
ヘッドホンとスピーカーを別々に記録する。2chであるだけでバイノーラルと呼ばない。仕様の違いから知覚や芸術性の優劣を自動判定しない。

## 実装・観察との接続
実装候補：自由なPythonコードで左右ゲインを計算。これは特定のWeb Audio実装の移植を要求しない。関連候補は生成時のnote IDと内容ハッシュから追跡する。人間の試聴結果はまだない。予想される効果は左右移動であり、実際に聞こえたかはレビューで確認する。
