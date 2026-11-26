# Solar Display 2 example

This example uses the iCS Home Assistant entities and the proxy’s shared control decisions. Each image carries
its render time and, when a charger button is available, the exact control revision or open Stop target.
The touchscreen requires a fresh download and a render less than 45 seconds old. A cached response can confirm
the download while the render continues to age. When either expires, the touchscreen shows its offline dashboard.
A new render can restore the dashboard after a Home Assistant clock correction. If the clock moves backward
and the renderer serves an older image again, a previously expired image can reappear.

The Home Assistant automation verifies the device, screen, frame age, position and original marker before sending
a request. Start also requires the current control revision and permission to start. Stop requires the target to
match the current open session. The touchscreen shows pending and unconfirmed feedback for the exact request from
the live Home Assistant helper, including between image downloads. While the automation is enabled, an
unconfirmed message clears on the first five-second cleanup tick after 20 seconds, or when a new request
replaces it. A charger tap must begin and end while the button is available and no request is pending.
When Home Assistant reports the automation as off or unavailable, the touchscreen immediately greys out both
charger buttons and ignores taps on them. If Home Assistant removes the automation without sending a state
update, the previous button image can remain until the next render or the offline timeout. Home Assistant
requires the automation to be installed and enabled to handle the tap. After reconnecting, the touchscreen
waits for fresh request feedback before accepting a charger tap. If two Home Assistant connections overlap,
the dashboard stays visible while its charger buttons are unavailable. After one connection closes, both
buttons wait for the request helper, automation state and active-run count from the remaining connection.
The offline dashboard hides request feedback.

If disabling the automation or restarting Home Assistant interrupts a Start or Stop wait, the first cleanup tick
after the automation resumes marks the saved request unconfirmed. The charger buttons wait for that feedback to
update before accepting another tap. The proxy can still settle the original request.

Use this with a compatible 480 × 480 ESPHome/LVGL touchscreen whose Home Assistant renderer provides the assigned
view and display image. The canvas example is a package for an existing board configuration. The board supplies its
display, touch and brightness components, its fallback dashboard, and the renderer integration.

1. Add the [request helper](request-helper.yaml) and [touch automation](controls.yaml) to Home Assistant.
   Set the actual ESPHome device ID, image entity, screen-source sensor and view ID in the automation.
2. Use [view.html.jinja](view.html.jinja) as the paired preview template. Its two optional vehicle cards expect
   the example battery and cable entity names near the top. Leave them unavailable or use local equivalents.
3. Adapt [esphome-canvas.yaml.example](esphome-canvas.yaml.example) to the board package and include
   [canvas_frame.h](canvas_frame.h). Keep the image URL and image-scoped authorization in local ESPHome secrets.
   The package synchronizes its Home Assistant clock every 10 seconds to check render times. Set the renderer to
   update every 25 seconds or less with the example's 10-second download interval and 5-second HTTP timeout.
   This leaves time for clock rounding and processing before the picture expires. Update the firmware, preview
   template and touch automation together. Keep the request helper and its options and remove `initial: idle`
   if an earlier example set it.
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
