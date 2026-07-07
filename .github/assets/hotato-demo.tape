# Renders docs/assets/hotato-demo.gif for the README.
#
# Reproduce:
#   go install github.com/charmbracelet/vhs@latest   # needs ffmpeg + ttyd + a Chromium
#   vhs .github/assets/hotato-demo.tape
#
# Real-call flow, not the synthetic battery: hotato scan finds candidate
# turn-taking moments in a real recorded call (a Vapi assistant on default
# interruption settings), hotato run scores the missed-interruption moment
# against a yield expectation and reports the measured FAIL, then hotato
# fixture create promotes that exact real moment into a permanent regression
# fixture. Every command below is verified to produce the pasted output
# before recording (see the task report). Same recording, same GIF, every
# time: this script is the deterministic source for the README's animated
# proof.

Output docs/assets/hotato-demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 680
Set Padding 20
Set Theme { "background": "#0e1a1b", "foreground": "#d6f2df", "black": "#0e1a1b", "red": "#ff6b6b", "green": "#D6F20F", "yellow": "#D6F20F", "cursor": "#D6F20F" }
Set TypingSpeed 45ms

Type "uvx hotato scan --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav"
Enter
Sleep 700ms

Type "uvx hotato run --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav --onset 2.00 --expect yield"
Enter
Sleep 700ms

Type "uvx hotato fixture create --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav --onset 2.00 --expect yield --id demo-missed-interrupt --out /tmp/hotato-demo-fixture"
Enter
Sleep 2s
