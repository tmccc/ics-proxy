# Solar Display 2 example

The Home Assistant integration publishes the eligible charger action, its generation, and request feedback in
`sensor.ics_proxy_display_command`. The display template sensor exposes that state while the touch automation is
enabled. A Start offer belongs to the current control revision and commissioned charger. A Stop offer also belongs
to the verified open session.

When a button is available, the renderer includes the action and generation in the image. The touchscreen enables
that button only while the image and live state agree. The pressed generation and original render time travel with
the tap. Home Assistant checks them again before sending the exact request to the proxy, including after opening
the management connection. The proxy retains authority for charger commands and their receipts.

Submitting a command retires the offer and shows pending feedback. After the exact receipt and a fresh status
refresh, an eligible action gets a new generation, even if its control revision is unchanged. If the result is
unconfirmed, live feedback lasts 20 elapsed seconds. An interrupted pending request that Home Assistant saved can
return as unconfirmed after reload or restart. A later command always uses a newly issued generation.

The touchscreen requires a fresh download and an image less than 45 seconds old. A cached response refreshes the
download time while the image continues to age. A Home Assistant clock step can temporarily withhold a tap until
a freshly rendered frame satisfies the action's time checks. During overlapping Home Assistant connections, the
dashboard can stay visible while its buttons wait for a fresh command view from the surviving connection.

## Installation and upgrade

Use a compatible 480 × 480 ESPHome/LVGL touchscreen. The board configuration supplies the display, touch and
brightness components, a fallback dashboard, and the Home Assistant renderer integration.

1. Add the `template` configuration from [request-helper.yaml](request-helper.yaml) and install the
   [touch automation](controls.yaml). Set the actual ESPHome device ID, image entity, screen-source sensor,
   and view ID in the automation. Point the template sensor at the installed automation and proxy command sensor.
2. Install [view.html.jinja](view.html.jinja) as the display preview template. The renderer needs the literal
   entity ID in its `states()` call to subscribe to the command view. Its optional vehicle cards can use the
   example entity names or local equivalents.
3. Adapt [esphome-canvas.yaml.example](esphome-canvas.yaml.example) to the board and include
   [canvas_frame.h](canvas_frame.h). Set `canvas_ics_view_entity` to the display template sensor's entity ID.
   Keep the image URL and image-scoped authorization in local ESPHome secrets. Set the renderer to update
   every 25 seconds or less with the example's 10-second download interval and 5-second HTTP timeout.
4. Update the integration, renderer, automation and firmware as a set. Once all four are present, confirm that
   the touchscreen can show an enabled button only when the rendered offer matches the live command view.
   The v2 footer retains the image timestamp in the earlier format so older firmware can still show the dashboard.
   An actionable v2 button requires the matching v2 command view. Existing Home Assistant clients can still use
   the earlier action fields. The old request helper and its expiry script can be removed after their last legacy
   display is upgraded.

The display calls `ics_proxy.session_command` with `action`, `canvas_generation`, and
`canvas_rendered_at_epoch`. The integration supplies the original revision and Stop target to the proxy.
If the display says **Charger buttons unavailable**, check that the automation is enabled, the template sensor
shows a v2 state, and the latest frame has downloaded.

Run the offline firmware regression with `python -B examples/solar-display-2/tests/test_canvas_frame.py`.
It uses `g++` with C++20 and tests the firmware header with a simulated clock.
