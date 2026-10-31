この Mac で BridgeBoard の iPad アプリを使いたいです。BridgeBoard server は、この Mac の Claude Code のセッションと、その会話・プロジェクトのファイルを、自分の Tailscale ネットワーク越しに iPad へ見せるための、読むだけの小さなプログラムです。下の手順どおりに入れてください。ソースは https://github.com/OneMoreLab-jp/bridgeboard-server（MIT ライセンス）です。この Mac で Tailscale にサインインしていて、私の tailnet で **MagicDNS が有効**であることが要ります（Tailscale の管理画面の DNS のページ）。手順2の確かめが `magicdns` として知らせます。

## 守ること

- 手順に書いたコマンドだけを、順番どおりに実行する。スクリプトや取得したファイルを書き換えない。失敗を、回り道をして「直さない」。
- コマンドが失敗したら、その出力をそのまま私に見せて、そこで止まる。条件を変えてやり直さない。
- sudo を使わない。パスワードを聞かない。ほかのもの（Homebrew・Tailscale・パッケージ）を入れない。ほかの設定を変えない。
- `~/Library/Application Support/BridgeBoard/` の中のファイルは、読まない・表示しない・写さない・検索しない・一覧にしない。中には秘密と私の端末の鍵があります。下のコマンドが自分でそこに書きます。
- サンドボックスや許可の決まりでコマンドが止められたとき（例：`~/Library/LaunchAgents` に書けない、`launchctl` が動かせない、github.com に届かない）は、回り道をしない。サンドボックスを切らない。どのコマンドが止められたかを伝え、そのコマンドを（実際のパスを入れた形で）私が自分のターミナルアプリで打つ1行として示して、私が済んだと言うまで待つ。

## 手順

1. リリース `v0.1.0` を新しい一時フォルダに取得する（私が開いているフォルダには取らない）。次の1つのコマンドが、コミットの ID と、取得したフォルダの絶対パスをこの順に出す。ID は私に伝える（GitHub のタグと私が見比べます）。パスは控える：

   ```
   CLONE="$(mktemp -d)/bridgeboard-server" && git clone --depth 1 --branch v0.1.0 https://github.com/OneMoreLab-jp/bridgeboard-server.git "$CLONE" && git -C "$CLONE" rev-parse HEAD && echo "$CLONE"
   ```

   シェルの変数や作業フォルダは、コマンドごとに引き継がれないことがある。そこで、以降のコマンドはどれも、手順1で出たパスへの `cd` から始める（`<手順1で出たパス>` のところに、そのパス自体を引用符つきで書く）。

2. この Mac を確かめる。項目ごとに `ok`・`warn`・`fail` を1行ずつ出す。何も変えない：

   ```
   cd "<手順1で出たパス>" && ./install.sh --check
   ```

   `fail` で始まる行が1つでもあれば（終了コードは 3）、出力の全部をそのまま私に見せて止まる。`fail` の行ごとに、私がどう直せばよいかを1文添える。自分では直さない。（`fail magicdns` の直し方は、Tailscale の管理画面の DNS のページ https://login.tailscale.com/admin/dns で MagicDNS を有効にすること。）`warn remote-control` の行は失敗ではない。先へ進み、iPad に出るのは Remote Control で動いている Claude Code のセッションだけなので、私が1つ起こす必要があることを伝える。

3. 入れる。サーバーを `~/Library/Application Support/BridgeBoard/` に写し、`~/Library/LaunchAgents/` に LaunchAgent を置いて起動する。サーバーが答えるまで最大15秒待つ。オプションは付けない：

   ```
   cd "<手順1で出たパス>" && ./install.sh
   ```

   0 以外で終わったら（4 はサーバーが答えなかった印で、出力に記録の末尾が入っている）、出力をそのまま見せて止まる。

4. BridgeBoard アプリを入れた iPad が手元にあるかを私に聞く。あれば組む：

   ```
   "$HOME/Library/Application Support/BridgeBoard/current/bbctl" pair
   ```

   コマンドは最初に、Tailscale がこの Mac の MagicDNS の名前を答えることと、サーバーが答えることを確かめる。だめなら符号を作る前に `error:` の行を出す（そのメッセージを私に見せて止まる）。そのあと `QR image: <パス>` を出し、その画像をこの Mac の画面に開き、iPad が読むまで最大5分待つ。そのあと `paired: <端末の名前>`・`expired`・`replaced: ...`（別の組む操作が始まった）のどれかを出す。最大5分動くので、シェルのツールのタイムアウトを 330 秒以上にするか、バックグラウンドで動かして出力を読む。

   - QR の画像は開かない・読まない・表示しない・説明しない。一度きりの組む符号が入っていて、この会話に入ってしまうため。私が画面から自分で読みます。
   - QR の画像を開けなかったという警告（`Open it yourself: <パス>`）が出たら、そのパスを私に伝える。私が自分で開きます。自分では開かない。
   - パスが出たらすぐに、「iPad で BridgeBoard を開いて、いまこの Mac の画面に出ている QR を読んでください」と私に伝える。
   - `paired:` が出たら私に伝える。`expired` か `replaced` が出たら私に伝え、私が頼んだときだけもう一度動かす。組めたら QR の窓は閉じてよい（符号は一度きりで5分で切れ、画像のファイルは消える）。

5. 数行で報告する：手順1のコミットの ID、`install.sh` が入れた版、`paired:` が出たか。出力のほかの部分は書かない。
