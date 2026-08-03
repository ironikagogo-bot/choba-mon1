"""実LINE接続の診断ツール（あなたのMacで実行する）。

役割:
  1. PC版LINEが起動しているか
  2. アクセシビリティ許可が下りているか
  3. トーク一覧を今どう読めているか（相手名・本文・未読）
  4. うまく読めないとき、LINEの画面構造(AXツリー)を書き出す
     → その出力を私に貼ってくれれば、読み取りセレクタを実機に合わせて調整します。

使い方（Macのターミナルで、choubaフォルダに移動してから）:
  python3 scripts/line_probe.py            # 読めているか確認
  python3 scripts/line_probe.py --dump      # AXツリーを浅く書き出す
  python3 scripts/line_probe.py --detail    # 一覧まわりを詳しく(全属性つき)書き出す
  python3 scripts/line_probe.py --detail > tree2.txt   # ファイルに保存して私に渡す
"""
import os
import sys

# このスクリプトはどこから実行されても動くよう、リポジトリ直下(appの親)をパスに足す。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    dump = "--dump" in sys.argv
    detail = "--detail" in sys.argv
    try:
        from app.watcher.mac_accessibility import (
            read_line_chat_list, dump_line_tree, dump_line_detail, SessionLostError)
    except Exception as e:
        print("❌ 読み取りモジュールを読み込めません:", e)
        print("   ヒント: chouba フォルダの中で実行していますか？")
        print("   例: cd ~/Downloads/chouba && python3 scripts/line_probe.py")
        return 1

    if "--force" in sys.argv:
        from app.watcher.mac_accessibility import force_accessibility
        import time
        print("=== 支援技術モードを強制ON → 再採取 ===")
        print(force_accessibility())
        time.sleep(1.0)
        print(dump_line_detail())
        print("=== ここまで。この出力をそのまま渡してください ===")
        return 0

    if detail:
        print("=== LINE 詳細ツリー(一覧まわり・全属性) ===")
        print(dump_line_detail())
        print("=== ここまで。この出力をそのまま渡してください ===")
        return 0

    if dump:
        print("=== LINE アクセシビリティ・ツリー(先頭400要素) ===")
        print(dump_line_tree())
        print("=== ここまで。この出力をそのまま渡してください ===")
        return 0

    try:
        rows = read_line_chat_list()
    except SessionLostError as e:
        print("❌", e)
        print("   確認: (1)PC版LINEを起動しログイン済みか (2)システム設定→プライバシーとセキュリティ→"
              "アクセシビリティ で、ターミナル(またはPython)に許可を与えているか")
        return 1
    except Exception as e:
        print("❌ 読み取り中にエラー:", e)
        print("   → `python3 scripts/line_probe.py --dump` の出力を渡してください。")
        return 1

    print(f"✓ LINEを検出。トーク一覧として {len(rows)} 行を読めました。\n")
    if not rows:
        print("  ただし中身が空です。--dump で構造を確認しましょう。")
    for r in rows[:20]:
        badge = f"[未読{r['unread']}]" if r.get("unread") else "      "
        print(f"  {badge} {r['contact']:<16} | {r['snippet'][:40]}")
    if len(rows) > 20:
        print(f"  …ほか {len(rows) - 20} 行")
    print("\nこの一覧が実際のLINEのトーク一覧と食い違う場合は、--dump 出力を渡してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
