# Renders docs/demo.gif.  Regenerate with:  vhs docs/demo.tape
# (run from the repo root after `uv sync`; needs the `vhs` package)
#
# Runs in a throwaway home under /tmp/backuper-demo, so it never touches your
# real manifest, state or journal.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1150
Set Height 430
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type `export PATH="$PWD/.venv/bin:$PATH" BACKUPER_SYSLOG=0 BACKUPER_HOST=laptop HOME=/tmp/backuper-demo/home XDG_CONFIG_HOME=/tmp/backuper-demo/home/.config XDG_STATE_HOME=/tmp/backuper-demo/home/.local/state XDG_RUNTIME_DIR=/tmp/backuper-demo/run` Enter
Type `rm -rf /tmp/backuper-demo && mkdir -p $XDG_RUNTIME_DIR ~/Nextcloud/backup ~/documents ~/projects/app/src ~/projects/app/build && cd ~` Enter
Type `echo "# notes" > documents/notes.md; echo "todo" > documents/todo.txt; head -c 1200000 /dev/urandom > documents/report.pdf; head -c 48000 /dev/urandom > projects/app/src/main.py; head -c 9000000 /dev/zero > projects/app/build/app.o` Enter
Type `export PS1='\[\e[1;34m\]~\[\e[0m\] $ '; clear` Enter
Show

Type "backuper setup ~/Nextcloud/backup --no-timer"
Sleep 300ms Enter
Sleep 1.2s

Type "backuper add ~/documents"
Sleep 300ms Enter
Sleep 1s
Type "backuper add ~/projects --exclude build"
Sleep 300ms Enter
Sleep 1.2s

Type "backuper ls --size"
Sleep 300ms Enter
Sleep 2.5s

Type "backuper backup"
Sleep 300ms Enter
Sleep 2.5s

Type "clear" Enter
Sleep 300ms
Type "rm ~/documents/notes.md   # oops"
Sleep 300ms Enter
Sleep 1s

Type "backuper restore --list"
Sleep 300ms Enter
Sleep 2s

Type "backuper restore ~/documents --yes"
Sleep 300ms Enter
Sleep 1.5s

Type "cat ~/documents/notes.md"
Sleep 300ms Enter
Sleep 1.2s

Type "backuper status"
Sleep 300ms Enter
Sleep 3s

Hide
Type "rm -rf /tmp/backuper-demo" Enter
