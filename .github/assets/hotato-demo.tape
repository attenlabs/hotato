# Renders docs/assets/hotato-demo.gif for the README.
#
# Reproduce:
#   go install github.com/charmbracelet/vhs@latest   # needs ffmpeg + ttyd + a Chromium
#   vhs .github/assets/hotato-demo.tape
#
# Same recording, same GIF, every time: this script is the deterministic
# source for the README's animated proof, in the same spirit as the tool
# itself (see .github/assets/record-demo.sh for the compare step it calls).

Output docs/assets/hotato-demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 680
Set Padding 20
Set Theme { "background": "#0e1a1b", "foreground": "#d6f2df", "black": "#0e1a1b", "red": "#ff6b6b", "green": "#D6F20F", "yellow": "#D6F20F", "cursor": "#D6F20F" }
Set TypingSpeed 45ms

Type "uvx hotato demo --no-open"
Enter
Sleep 5s

Type "./.github/assets/record-demo.sh"
Enter
Sleep 4s

Sleep 2s
