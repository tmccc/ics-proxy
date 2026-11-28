#include <cassert>
#include <limits>

#include "canvas_frame.h"

using solar_display::CanvasFreshness;
using solar_display::CanvasControlState;
using solar_display::CanvasControlView;
using solar_display::ChargerButton;
using solar_display::ChargerFeedback;
using solar_display::parse_canvas_marker;

void publish(CanvasFreshness &frame, uint32_t rendered, uint32_t received_ms, int64_t wall) {
  assert(frame.accepts(rendered, received_ms, wall));
  frame.publish_render(rendered, received_ms, wall);
  frame.note_fetch(received_ms);
}

int main() {
  CanvasFreshness displayed;
  publish(displayed, 1000, 1000, 1000);
  assert(displayed.fresh(1500, 1000));
  auto pressed = displayed;

  // A refresh leaves the copied press tied to its original render.
  publish(displayed, 1002, 3000, 1002);
  assert(pressed.rendered_at() == 1000);
  assert(pressed.render_age_ms(3500, 1002) == 3500);
  assert(displayed.rendered_at() == 1002);

  // The newer render is still fresh even after the pressed frame's elapsed deadline.
  publish(displayed, 1043, 44000, 1043);
  assert(pressed.render_age_ms(44999, 1043) == 44999);
  assert(pressed.render_age_ms(45000, 1043) == CanvasFreshness::MAX_AGE_MS);
  assert(displayed.fresh(45000, 1043));

  // A cache response renews the fetch deadline without giving the old render a new deadline.
  displayed.note_fetch(87000);
  assert(!displayed.fresh(89000, 1087));

  // The elapsed age of a pressed frame continues across the 32-bit millis() rollover.
  constexpr auto last_ms = std::numeric_limits<uint32_t>::max();
  CanvasFreshness before_wrap;
  publish(before_wrap, 2000, last_ms - 499, 2000);
  assert(before_wrap.fresh(last_ms - 199, 2000));
  auto pressed_before_wrap = before_wrap;
  publish(before_wrap, 2001, 250, 2001);
  assert(pressed_before_wrap.rendered_at() == 2000);
  assert(pressed_before_wrap.render_age_ms(500, 2001) == 2000);

  const std::string first(32, 'a'), next(32, 'b');
  const std::string read_one(32, 'c'), read_two(32, 'd'), read_three(32, 'e');
  const std::string start = "v2:start:" + first;
  const std::string stop = "v2:stop:" + next;
  const std::string start_view = "v2:idle:start:" + first;
  const std::string stop_view = "v2:idle:stop:" + next;
  const auto older = parse_canvas_marker("d51a1000003e8" + std::string(14, '0'));
  const auto start_image = parse_canvas_marker("d51a0000003e8d52a1" + first);
  const auto stop_image = parse_canvas_marker("d51a0000003e9d52a2" + next);
  assert(older.rendered_at == 1000 && older.control == "legacy");
  assert(start_image.rendered_at == 1000 && start_image.control == start);
  assert(stop_image.rendered_at == 1001 && stop_image.control == stop);
  assert(parse_canvas_marker("d51a0000003e8").control.empty());
  assert(parse_canvas_marker("d51a0000003e8d52a1").control == "invalid");
  assert(parse_canvas_marker("d51a0000003ezd52a1" + first).rendered_at == 0);
  assert(parse_canvas_marker("d51a0000003e8d52a1" + std::string(32, 'A')).control == "invalid");
  assert(CanvasControlState::parse("v2:idle:none:-").button == ChargerButton::NONE);
  assert(CanvasControlState::parse("v2:idle:none:" + first).feedback == ChargerFeedback::UNKNOWN);
  assert(CanvasControlState::parse("v2:idle:start:-").feedback == ChargerFeedback::UNKNOWN);

  CanvasControlView view;
  view.observe(start_view);
  assert(view.allows(ChargerButton::START, start_image.control));
  assert(!view.allows(ChargerButton::STOP, start_image.control));
  assert(!view.allows(ChargerButton::START, older.control));
  view.submitted(start, last_ms - 499);
  view.observe(start_view);
  assert(!view.allows(ChargerButton::START, start));
  assert(!view.needs_refresh(2499) && view.needs_refresh(2500));
  view.request_refresh(read_one, 2500);
  assert(!view.needs_refresh(2501));
  view.observe(start_view);
  view.observe_refresh(read_two + ':' + start_view);
  assert(!view.allows(ChargerButton::START, start));
  view.observe_refresh(read_one + ':' + start_view);
  assert(view.allows(ChargerButton::START, start));

  // Pending feedback wins even if a same-generation reply is in flight.
  view.submitted(start, 3000);
  view.request_refresh(read_two, 6000);
  view.observe("v2:starting:none:-");
  view.observe_refresh(read_two + ':' + start_view);
  assert(view.state().feedback == ChargerFeedback::STARTING);
  assert(!view.allows(ChargerButton::START, start));
  view.invalidate();
  view.observe(start_view);
  assert(!view.needs_refresh(12000) && !view.allows(ChargerButton::START, start));
  view.observe("v2:starting_timeout:stop:" + next);
  assert(!view.allows(ChargerButton::START, start));
  assert(view.allows(ChargerButton::STOP, stop_image.control));
  view.observe(start_view);
  assert(!view.allows(ChargerButton::START, start));
  view.observe(stop_view);

  // The surviving socket's queued state and any reply to a previous read cannot reopen an old button.
  CanvasControlView reconnect;
  reconnect.invalidate(true);
  assert(reconnect.needs_refresh(13000));
  reconnect.request_refresh(read_one, 13000);
  reconnect.observe(stop_view);
  assert(reconnect.state().feedback == ChargerFeedback::UNKNOWN && !reconnect.allows(ChargerButton::STOP, stop));
  assert(!reconnect.needs_refresh(15999) && reconnect.needs_refresh(16000));
  reconnect.request_refresh(read_two, 16000);
  reconnect.observe_refresh(read_one + ':' + stop_view);
  reconnect.observe_refresh(read_two + ':' + "malformed");
  assert(!reconnect.allows(ChargerButton::STOP, stop));
  reconnect.invalidate(true);
  reconnect.request_refresh(read_three, 16500);
  reconnect.observe_refresh(read_two + ':' + stop_view);
  assert(!reconnect.allows(ChargerButton::STOP, stop));
  reconnect.observe_refresh(read_three + ':' + start_view);
  assert(!reconnect.allows(ChargerButton::STOP, stop) && reconnect.allows(ChargerButton::START, start));
  reconnect.observe(stop_view);
  assert(reconnect.allows(ChargerButton::STOP, stop));

  // A reconnect read sent shortly after a tap cannot also serve as its admission recheck.
  reconnect.submitted(stop, 17000);
  reconnect.invalidate(true);
  reconnect.request_refresh(read_one, 17100);
  reconnect.observe_refresh(read_one + ':' + stop_view);
  assert(!reconnect.needs_refresh(19999) && reconnect.needs_refresh(20000));
  reconnect.request_refresh(read_two, 20000);
  reconnect.observe(stop_view);
  reconnect.observe_refresh(read_one + ':' + stop_view);
  assert(!reconnect.allows(ChargerButton::STOP, stop));
  reconnect.observe_refresh(read_two + ':' + stop_view);
  assert(reconnect.allows(ChargerButton::STOP, stop));
  reconnect.observe("v2:unavailable:none:-");
  assert(!reconnect.allows(ChargerButton::STOP, stop));
  const std::string after_reconnect(32, 'c');
  reconnect.observe("v2:idle:stop:" + after_reconnect);
  assert(!reconnect.allows(ChargerButton::STOP, stop_image.control));
  assert(reconnect.allows(ChargerButton::STOP, "v2:stop:" + after_reconnect));
}
