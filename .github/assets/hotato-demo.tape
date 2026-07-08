# Renders docs/assets/hotato-demo.gif for the README.
#
# Reproduce (recommended): scripts/record_demo_gif.sh
#   That wrapper builds a local `uvx hotato` shim (hotato is not published),
#   cleans the fixture output dir, GUARDS that the battery ends on a success
#   (never a raw `error:` line), then runs vhs on this tape. It needs:
#     go install github.com/charmbracelet/vhs@latest   # + ffmpeg, ttyd, Chromium
#
# Real-call flow, not the synthetic battery: hotato scan finds candidate
# turn-taking moments in a real recorded call (an operator-recorded probe
# against a voice agent on default interruption settings), hotato run scores
# the missed-interruption moment against a yield expectation and reports the
# measured FAIL, then hotato fixture create promotes that exact real moment
# into a permanent regression fixture. Same recording, same GIF, every time:
# this tape is the deterministic source for the README's animated proof. The
# final frame is always the fixture-create SUCCESS, never an error.

Output docs/assets/hotato-demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 680
Set Padding 20
Set Theme { "background": "#0e1a1b", "foreground": "#d6f2df", "black": "#0e1a1b", "red": "#ff6b6b", "green": "#D6F20F", "yellow": "#D6F20F", "cursor": "#D6F20F" }
Set TypingSpeed 10ms

# Hidden setup: clear the fixture output dir so the recorded create SUCCEEDS
# (a stale dir is what made the old GIF end on "error: ... already exists"),
# and clear the screen so the first visible frame is the prompt, not shell
# init noise. Hide stops frame capture, so this adds nothing to the GIF.
Hide
Type "rm -rf /tmp/hotato-demo-fixture && clear"
Enter
Sleep 250ms
Show

Type "uvx hotato scan --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav"
Enter
Sleep 1000ms

Type "uvx hotato run --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav --onset 2.00 --expect yield"
Enter
Sleep 1300ms

Type "uvx hotato fixture create --stereo corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav --onset 2.00 --expect yield --id demo-missed-interrupt --out /tmp/hotato-demo-fixture"
Enter
Sleep 2000ms
