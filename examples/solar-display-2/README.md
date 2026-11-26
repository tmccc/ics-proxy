# Solar Display 2 example

This example uses the iCS Home Assistant entities and the proxy’s shared control decisions. Each image carries
its render time and, when a charger button is available, the exact control revision or open Stop target.
The touchscreen requires a fresh download and a render less than 45 seconds old. A cached response can confirm
the download while the render continues to age. When either expires, the touchscreen shows its offline dashboard.
A new render can restore the dashboard after a Home Assistant clock correction. If the clock moves backward
and the renderer serves an older image again, a previously expired image can reappear.

The Home Assistant automation verifies the device, screen, position and original marker before sending a request.
It passes the render time of the frame shown when the button was pressed to the iCS action, which also checks its
age against elapsed time. A refresh with the same control leaves that press bound to the original render time.
The action requires a new frame after the current iCS integration entry loads or first observes a different
commissioned destination, including a replacement proxy at the same address. When it detects a clock step in
either direction, it refuses that tap and requires a frame timestamp beyond both observed clock readings. The
charger buttons may appear available while the clock catches up and the display receives a new frame. The action
samples the clock when it handles the tap and again before writing. Clock adjustments that cancel out between
samples can leave the offset unchanged.
Start also requires the displayed control revision to match the current revision and permission to start.
Stop requires the displayed target to match a fresh, verified open session. The action checks the pressed frame
and current control again before sending the management request. It sends the opaque `canvas_owner` marker from
authenticated proxy status on each canvas Start or Stop. The proxy checks the marker before admitting the request.
The touchscreen shows pending and unconfirmed feedback for the exact request from the live Home Assistant helper,
including between image downloads. While the automation is enabled, an unconfirmed message clears after 20
elapsed seconds or when a new request replaces it. Home Assistant clock corrections do not extend the delay.
A charger tap must begin and end while the button is available and no request is pending.
When Home Assistant reports the automation as off or unavailable, the touchscreen immediately greys out both
charger buttons and ignores taps on them. If Home Assistant removes the automation without sending a state
update, the previous button image can remain until the next render or the offline timeout. Home Assistant
requires the automation to be installed and enabled to handle the tap. After reconnecting, the touchscreen
waits for fresh request feedback before accepting a charger tap. If two Home Assistant connections overlap,
the dashboard stays visible while its charger buttons are unavailable. After one connection closes, both
buttons wait for the request helper, automation state and active-run count from the remaining connection.
The offline dashboard hides request feedback.

If disabling the automation or restarting Home Assistant interrupts a Start or Stop wait, the first cleanup tick
after the automation resumes marks a saved pending request unconfirmed. The charger buttons wait for that feedback
to update before accepting another tap. Home Assistant clears restored unconfirmed messages on startup when the
automation is enabled, or at the first cleanup tick after it is enabled. An abrupt exit can lose display feedback
that Home Assistant has yet to save. The proxy can still settle the original request.

Use this with a compatible 480 × 480 ESPHome/LVGL touchscreen whose Home Assistant renderer provides the assigned
view and display image. The canvas example is a package for an existing board configuration. The board supplies its
display, touch and brightness components, its fallback dashboard, and the renderer integration.

1. Merge both the `input_select` and `script` entries from [request-helper.yaml](request-helper.yaml) into your
   Home Assistant configuration and install the [touch automation](controls.yaml).
   Set the actual ESPHome device ID, image entity, screen-source sensor and view ID in the automation.
2. Use [view.html.jinja](view.html.jinja) as the paired preview template. Its two optional vehicle cards expect
   the example battery and cable entity names near the top. Leave them unavailable or use local equivalents.
3. Adapt [esphome-canvas.yaml.example](esphome-canvas.yaml.example) to the board package and include
   [canvas_frame.h](canvas_frame.h). Keep the image URL and image-scoped authorization in local ESPHome secrets.
   The package synchronizes its Home Assistant clock every 10 seconds to check render times. Set the renderer to
   update every 25 seconds or less with the example's 10-second download interval and 5-second HTTP timeout.
   This leaves time for clock rounding and processing before the picture expires. Update the firmware, preview
   template, touch automation and iCS Home Assistant integration together so the render timestamp is checked
   through the action. Update the managed proxy and iCS Home Assistant integration for destination binding.
   A canvas action needs a valid `canvas_owner` marker in status, so an older proxy leaves a tap unconfirmed.
   Older management clients can omit the marker. When upgrading an earlier
   example, add the feedback expiry script from [request-helper.yaml](request-helper.yaml), keep the request
   helper and its options, and remove `initial: idle` if the earlier example set it.
4. Confirm that the automation entity ID used by the view and the firmware's `canvas_ics_automation_entity`
   substitution matches the installed automation.
   In the view, write the quoted entity ID directly in both `is_state()` and `state_attr()` so GeekMagic
   loads the automation’s state. Enable the automation in Home Assistant.

Start calls `ics_proxy.session_command` with `action: start` and the displayed revision. Stop calls it with
`action: stop_session` and the displayed session target. The client returns confirmation for the exact request.
The example leaves charging decisions, retries, and recovery with the proxy.

If Home Assistant offers Start but the display says **Charger buttons unavailable**, check that the touch
automation is enabled, its entity ID matches both calls in the template, and the request helper is available.
Once the frame refreshes, the display should say **Ready to start** when the proxy allows Start and no request is pending.

Run the offline C++ freshness regression with `python -B examples/solar-display-2/tests/test_canvas_frame.py`.
It uses `g++` with C++20 and exercises the actual firmware header with a simulated clock.
