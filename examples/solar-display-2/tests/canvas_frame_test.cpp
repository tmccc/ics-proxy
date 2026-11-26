#include <cassert>
#include <limits>

#include "canvas_frame.h"

using solar_display::CanvasFreshness;
using solar_display::CanvasRequestFeedback;

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

  constexpr auto start = "start:00000000000001";
  constexpr auto stop = "stop:current-session";
  constexpr auto new_stop = "stop:next-session";
  CanvasRequestFeedback feedback;
  feedback.submitted(start);
  feedback.observe("idle", 3000, start, 1000, 3000);
  assert(!feedback.retired(3000, start, 1000, 3000));
  feedback.observe("starting", 3000, start, 1500, 3000);
  assert(!feedback.retired(3000, start, 1500, 3000));
  // A refresh of the same control can arrive during the wait for confirmation.
  feedback.observe("idle", 3001, start, 4000, 3003);
  assert(feedback.retired(3001, start, 4000, 3003));
  // Frames rendered before confirmation can arrive later than the frame already displayed.
  assert(feedback.retired(3002, start, 5000, 3004));
  assert(!feedback.retired(3004, start, 5000, 3004));
  assert(!feedback.retired(3001, stop, 4000, 3003));
  feedback.reset_pending();
  feedback.observe("idle", 3001, start, 5000, 3004);
  assert(feedback.retired(3001, start, 5000, 3004));

  feedback.submitted(stop);
  feedback.observe("stopping", 3004, stop, 5000, 3004);
  feedback.observe("stopping_timeout", 3004, stop, 6000, 3005);
  feedback.observe("idle", 3004, stop, 7000, 3006);
  assert(!feedback.retired(3004, stop, 7000, 3006));

  feedback.submitted(stop);
  feedback.observe("stopping", 3004, stop, 7000, 3006);
  feedback.observe("idle", 3007, new_stop, 9000, 3008);
  assert(!feedback.retired(3007, new_stop, 9000, 3008));
  assert(feedback.retired(3007, stop, 9000, 3008));

  feedback.submitted(stop);
  feedback.observe("stopping", 5000, stop, last_ms - 999, 5010);
  feedback.observe("idle", 5000, stop, last_ms - 499, 5010);
  assert(feedback.retired(5004, stop, 500, 5011));
  // A backward clock change discards the time bound, preserving the known exact frame.
  assert(!feedback.retired(5004, stop, 1500, 5005));
  assert(feedback.retired(5000, stop, 1500, 5005));
  assert(!feedback.retired(5004, stop, 2000, 5012));
}
