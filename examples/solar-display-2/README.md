# Solar Display 2 example

This example uses the iCS Home Assistant entities and the proxy’s shared control decisions. The image carries
a marker for the exact control revision or open Stop target. The firmware checks that marker when touched.
The Home Assistant automation verifies the device, screen, frame age, position and original marker before sending
a request. Pending feedback comes from Home Assistant. An unconfirmed message clears after about 20 seconds.

Use this with a compatible 480 × 480 ESPHome/LVGL touchscreen whose Home Assistant renderer provides the assigned
view and display image. The canvas example is a package for an existing board configuration. The board supplies its
display, touch and brightness components, its fallback dashboard, and the renderer integration.

1. Add the [request helper](request-helper.yaml) and [touch automation](controls.yaml) to Home Assistant.
   Set the actual ESPHome device ID, image entity, screen-source sensor and view ID in the automation.
2. Use [view.html.jinja](view.html.jinja) as the paired preview template. Its two optional vehicle cards expect
   the example battery and cable entity names near the top. Leave them unavailable or use local equivalents.
3. Adapt [esphome-canvas.yaml.example](esphome-canvas.yaml.example) to the board package and include
   [canvas_frame.h](canvas_frame.h). Keep the image URL and image-scoped authorization in local ESPHome secrets.
4. Confirm that the automation entity ID used by the view and firmware matches the installed automation.

Start calls `ics_proxy.session_command` with `action: start` and the displayed revision. Stop calls it with
`action: stop_session` and the displayed session target. The client returns confirmation for the exact request.
The example leaves charging decisions, retries, and recovery with the proxy.
