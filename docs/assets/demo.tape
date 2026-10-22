# vhs docs/assets/demo.tape   (from the repo root; needs `brew install vhs`)
Output docs/assets/demo.gif
Set Shell bash
Set FontSize 17
Set Width 1240
Set Height 620
Set Padding 20
Set Theme { "background": "#0b0b0b", "foreground": "#39ff6a", "cursor": "#ffd000", "green": "#39ff6a", "yellow": "#ffd000", "red": "#ff4d6d", "blue": "#ffd000", "brightBlack": "#8a8a8a", "white": "#f0f0f0" }
Set TypingSpeed 35ms

Hide
Type "source scripts/demo-env.sh; PS1='$ '; clear"
Enter
Sleep 1s
Show

Type "damien plan build -q 'in:inbox' --show 6"
Sleep 400ms
Enter
Sleep 4s

Hide
Type "PLAN=$(demo_plan); clear"
Enter
Sleep 800ms
Type "damien plan approve $PLAN"
Escape
Ctrl+E
Sleep 300ms
Show
Sleep 600ms
Enter
Sleep 2500ms

Hide
Type "TOKEN=$(demo_token $PLAN); clear"
Enter
Sleep 800ms
Type "damien plan apply $PLAN --token $TOKEN --yes --only trash"
Escape
Ctrl+E
Sleep 300ms
Show
Sleep 600ms
Enter
Sleep 3500ms

Hide
Type "RUN=$(demo_run $PLAN); clear"
Enter
Sleep 800ms
Type "damien run undo $RUN --yes"
Escape
Ctrl+E
Sleep 300ms
Show
Sleep 600ms
Enter
Sleep 3s
