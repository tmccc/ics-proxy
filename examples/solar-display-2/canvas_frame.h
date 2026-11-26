#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <string_view>

#include "esphome/components/image/image.h"
#include "esphome/core/helpers.h"

#ifdef USE_LVGL
#include <src/misc/cache/instance/lv_image_cache.h>
#endif

namespace solar_display {

enum class ChargerButton { NONE, START, STOP };

enum ChargerFeedback : uint8_t {
  REQUEST_STATE = 1 << 0,
  AUTOMATION_STATE = 1 << 1,
  ACTIVE_RUNS = 1 << 2,
  ALL_FEEDBACK = REQUEST_STATE | AUTOMATION_STATE | ACTIVE_RUNS,
};

// A deliberate tap starts and ends inside the same charger button.
inline ChargerButton charger_button_at(int x, int y) {
  if (y < 366 || y >= 428) return ChargerButton::NONE;
  if (x >= 20 && x < 232) return ChargerButton::START;
  if (x >= 248 && x < 460) return ChargerButton::STOP;
  return ChargerButton::NONE;
}

inline bool is_home_assistant_client(std::string_view name) {
  return name.starts_with("Home Assistant");
}

// Match the Home Assistant touch automation's eligible feedback states.
inline bool charger_request_idle(const std::string &request, float active_runs) {
  return active_runs == 0 &&
         (request == "idle" || request == "starting_timeout" || request == "stopping_timeout");
}

inline bool charger_request_known(const std::string &request) {
  return charger_request_idle(request, 0) || request == "starting" || request == "stopping";
}

struct CanvasMarker {
  uint32_t rendered_at{0};
  std::string control;
};

// Keep a confirmed tap from re-enabling the control already painted into the image.
class CanvasRequestFeedback {
 public:
  void submitted(std::string_view control) {
    this->submitted_ = control;
    this->pending_ = false;
  }

  void reset_pending() {
    this->submitted_.clear();
    this->pending_ = false;
  }

  void observe(std::string_view request, uint32_t rendered_at, std::string_view control, uint32_t now, int64_t epoch) {
    if (request == "starting" || request == "stopping") {
      this->pending_ = this->submitted_.starts_with(request == "starting" ? "start:" : "stop:");
      return;
    }
    // Idle without a pending transition can also be the initial or a repeated subscription value.
    if (request == "idle" && !this->pending_) return;
    if (request == "idle") {
      this->retired_ = {control == this->submitted_ ? rendered_at : 0, this->submitted_};
      this->confirmed_ms_ = now;
      this->confirmed_epoch_ = epoch;
    }
    this->reset_pending();
  }

  bool retired(uint32_t rendered_at, std::string_view control, uint32_t now, int64_t epoch) {
    // Keep the exact consumed frame across a rewind, but allow new frames on the new clock sequence.
    // Allow for separate rounding of elapsed seconds and the synchronized wall clock.
    if (this->confirmed_epoch_ > 0 && epoch > 0 &&
        epoch + 1 < this->confirmed_epoch_ + (now - this->confirmed_ms_) / 1000) {
      this->confirmed_epoch_ = 0;
    }
    return rendered_at != 0 && control == this->retired_.control &&
           (rendered_at == this->retired_.rendered_at || rendered_at <= this->confirmed_epoch_);
  }

 private:
  std::string submitted_;
  bool pending_{false};
  CanvasMarker retired_;
  uint32_t confirmed_ms_{0};
  int64_t confirmed_epoch_{0};
};

// The server's clock supplies the age at download. Elapsed time keeps that
// frame aging even if a cache response or clock correction arrives later.
class CanvasFreshness {
 public:
  static constexpr uint32_t MAX_AGE_MS = 45000;

  bool accepts(uint32_t rendered_at, uint32_t now, int64_t epoch) {
    if (rendered_at == this->rendered_at_) return this->render_age_ms(now, epoch) < MAX_AGE_MS;
    if (wall_age_ms_(rendered_at, epoch) >= MAX_AGE_MS) return false;
    // A previous valid marker now in the future proves the clock moved back.
    // A fresh, different marker can then establish a new render sequence.
    return rendered_at > this->rendered_at_ || epoch < static_cast<int64_t>(this->rendered_at_) - 1;
  }

  // Call only after those exact image bytes have been committed.
  void publish_render(uint32_t rendered_at, uint32_t now, int64_t epoch) {
    if (rendered_at == this->rendered_at_) return;
    this->rendered_at_ = rendered_at;
    this->age_checked_ms_ = now;
    this->render_age_ms_ = wall_age_ms_(rendered_at, epoch);
  }

  void note_fetch(uint32_t now) { this->last_fetch_ms_ = now; }

  uint32_t rendered_at() const { return this->rendered_at_; }

  uint32_t render_age_ms(uint32_t now, int64_t epoch) {
    if (this->render_age_ms_ >= MAX_AGE_MS) return MAX_AGE_MS;
    const uint32_t elapsed = std::min(now - this->age_checked_ms_, MAX_AGE_MS);
    const uint32_t wall_age = wall_age_ms_(this->rendered_at_, epoch);
    // Each check carries the frame's age forward, including across clock corrections.
    this->render_age_ms_ = std::min(std::max(wall_age, this->render_age_ms_ + elapsed), MAX_AGE_MS);
    this->age_checked_ms_ = now;
    return this->render_age_ms_;
  }

  bool fresh(uint32_t now, int64_t epoch) {
    return this->render_age_ms(now, epoch) < MAX_AGE_MS && now - this->last_fetch_ms_ < MAX_AGE_MS;
  }

 private:
  static uint32_t wall_age_ms_(uint32_t rendered_at, int64_t epoch) {
    // Allow one second of clock rounding while keeping the age conservative.
    const int64_t render_epoch = rendered_at;
    if (rendered_at == 0 || epoch < render_epoch - 1 ||
        epoch >= render_epoch + MAX_AGE_MS / 1000 - 1) return MAX_AGE_MS;
    return static_cast<uint32_t>(epoch - render_epoch + 1) * 1000;
  }

  uint32_t rendered_at_{0};
  uint32_t age_checked_ms_{0};
  uint32_t render_age_ms_{MAX_AGE_MS};
  uint32_t last_fetch_ms_{0};
};

// The downloader may repaint its decode buffer while the previous picture is
// visible. Keep the displayed picture in independent PSRAM and publish it only
// after the downloader has completed a valid frame.
class StableCanvasFrame : public esphome::image::Image {
 public:
  enum class CommitResult { INVALID, UNCHANGED, CHANGED };

  struct DirtyRegion {
    int x1{0};
    int y1{0};
    int x2{-1};
    int y2{-1};
    bool valid() const { return this->x2 >= this->x1 && this->y2 >= this->y1; }
  };

  static constexpr int WIDTH = 480;
  static constexpr int HEIGHT = 480;
  static constexpr size_t FRAME_BYTES = static_cast<size_t>(WIDTH) * HEIGHT * 2;
  static constexpr int BAND_HEIGHT = 16;
  static constexpr size_t DIRTY_REGION_COUNT = 30;
  static_assert(HEIGHT == BAND_HEIGHT * DIRTY_REGION_COUNT);

  StableCanvasFrame()
      : Image(BLANK_PIXEL, 1, 1, esphome::image::IMAGE_TYPE_RGB565,
              esphome::image::TRANSPARENCY_OPAQUE) {}

  ~StableCanvasFrame() {
    if (this->front_ != nullptr) {
      esphome::RAMAllocator<uint8_t> allocator(esphome::RAMAllocator<uint8_t>::ALLOC_EXTERNAL);
      allocator.deallocate(this->front_, FRAME_BYTES);
    }
  }

  StableCanvasFrame(const StableCanvasFrame &) = delete;
  StableCanvasFrame &operator=(const StableCanvasFrame &) = delete;
  StableCanvasFrame(StableCanvasFrame &&) = delete;
  StableCanvasFrame &operator=(StableCanvasFrame &&) = delete;

  CommitResult commit(const esphome::image::Image &source) {
    this->dirty_regions_.fill(DirtyRegion{});
    this->last_copy_bytes_ = 0;
    if (!valid_picture_(source)) return CommitResult::INVALID;

    const uint8_t *pixels = source.get_data_start();
    if (this->front_ != nullptr) {
      if (!this->find_dirty_regions_(pixels)) {
        return CommitResult::UNCHANGED;
      }
      // Reuse the regions already found for LVGL invalidation. Small value
      // changes should not copy a full frame while RGB scanout reads PSRAM.
      constexpr size_t row_bytes = static_cast<size_t>(WIDTH) * 2;
      for (const auto &region : this->dirty_regions_) {
        if (!region.valid()) continue;
        const size_t copy_bytes = static_cast<size_t>(region.x2 - region.x1 + 1) * 2;
        for (int y = region.y1; y <= region.y2; ++y) {
          const size_t offset = static_cast<size_t>(y) * row_bytes + region.x1 * 2;
          std::memcpy(this->front_ + offset, pixels + offset, copy_bytes);
          this->last_copy_bytes_ += copy_bytes;
        }
      }
    } else {
      // Never consume the smaller internal heap if external RAM is exhausted.
      esphome::RAMAllocator<uint8_t> allocator(esphome::RAMAllocator<uint8_t>::ALLOC_EXTERNAL);
      uint8_t *new_front = allocator.allocate(FRAME_BYTES);
      if (new_front == nullptr) {
        return CommitResult::INVALID;
      }
      std::memcpy(new_front, pixels, FRAME_BYTES);
      this->last_copy_bytes_ = FRAME_BYTES;
      this->front_ = new_front;
      this->data_start_ = new_front;
      this->width_ = WIDTH;
      this->height_ = HEIGHT;
      this->dirty_regions_[0] = {0, 0, WIDTH - 1, HEIGHT - 1};
#ifdef USE_LVGL
      // The initial blank may already have a cached1x1 LVGL descriptor.
      // Force Image::get_lv_image_dsc() to construct the full-frame descriptor.
      this->dsc_ = {};
#endif
    }

    ++this->commit_count_;
    return CommitResult::CHANGED;
  }

  // Read the timestamp and control target from the same image before displaying it.
  static CanvasMarker read_marker(const esphome::image::Image &source) {
    if (!valid_picture_(source)) return {};
    constexpr char hex[]{"0123456789abcdef"};
    std::string marker;
    constexpr int header_cells = 5;
    constexpr int render_cells = header_cells + 8;
    int cells = render_cells;
    uint32_t rendered_at = 0;
    for (int i = 0; i < cells; ++i) {
      const auto pixel = source.get_pixel(7 + 6 * i, 477);
      const int r = (pixel.r >> 6);
      const int g = (pixel.g >> 6);
      // The renderer resamples the PNG. Allow ringing around each cell's colour.
      if (pixel.b < 40 || pixel.b > 74 ||
          pixel.r < 8 + r * 64 || pixel.r > 56 + r * 64 ||
          pixel.g < 8 + g * 64 || pixel.g > 56 + g * 64) return {};
      const int digit = 4 * r + g;
      marker += hex[digit];
      if (i == 3 && marker != "d51a") return {};
      if (i == 4) {
        if (marker == "d51a1") cells += 14;
        else if (marker == "d51a2") cells += 64;
        else if (marker != "d51a0") return {};
      }
      if (header_cells <= i && i < render_cells) rendered_at = (rendered_at << 4) | digit;
    }
    if (marker[4] == '0') return {rendered_at, {}};
    return {rendered_at, (marker[4] == '1' ? "start:" : "stop:") + marker.substr(render_cells)};
  }

  bool ready() const { return this->front_ != nullptr; }
  uint32_t commit_count() const { return this->commit_count_; }
  size_t last_copy_bytes() const { return this->last_copy_bytes_; }
  const std::array<DirtyRegion, DIRTY_REGION_COUNT> &dirty_regions() const { return this->dirty_regions_; }
  CanvasFreshness &freshness() { return this->freshness_; }
  CanvasFreshness &pressed_freshness() { return this->pressed_freshness_; }
  CanvasRequestFeedback &request_feedback() { return this->request_feedback_; }

  bool control_retired(std::string_view control, uint32_t now, int64_t epoch) {
    return this->request_feedback_.retired(this->freshness_.rendered_at(), control, now, epoch);
  }

 protected:
  static bool valid_picture_(const esphome::image::Image &source) {
    return source.get_width() == WIDTH && source.get_height() == HEIGHT &&
           source.get_type() == esphome::image::IMAGE_TYPE_RGB565 && !source.has_transparency() &&
           source.get_data_start() != nullptr && source.get_width_stride() == static_cast<size_t>(WIDTH) * 2;
  }

  bool find_dirty_regions_(const uint8_t *pixels) {
    constexpr size_t row_bytes = static_cast<size_t>(WIDTH) * 2;
    constexpr size_t band_bytes = row_bytes * BAND_HEIGHT;
    bool changed = false;
    for (size_t band = 0; band < DIRTY_REGION_COUNT; ++band) {
      const size_t offset = band * band_bytes;
      if (std::memcmp(this->front_ + offset, pixels + offset, band_bytes) == 0) {
        continue;
      }
      DirtyRegion region{WIDTH, HEIGHT, -1, -1};
      const int first_row = static_cast<int>(band) * BAND_HEIGHT;
      for (int y = first_row; y < first_row + BAND_HEIGHT; ++y) {
        const uint8_t *old_row = this->front_ + static_cast<size_t>(y) * row_bytes;
        const uint8_t *new_row = pixels + static_cast<size_t>(y) * row_bytes;
        if (std::memcmp(old_row, new_row, row_bytes) == 0) {
          continue;
        }
        int first = 0;
        while (first < WIDTH && old_row[first * 2] == new_row[first * 2] &&
               old_row[first * 2 + 1] == new_row[first * 2 + 1]) {
          ++first;
        }
        int last = WIDTH - 1;
        while (last > first && old_row[last * 2] == new_row[last * 2] &&
               old_row[last * 2 + 1] == new_row[last * 2 + 1]) {
          --last;
        }
        if (first < region.x1) region.x1 = first;
        if (last > region.x2) region.x2 = last;
        if (y < region.y1) region.y1 = y;
        region.y2 = y;
      }
      this->dirty_regions_[band] = region;
      changed = true;
    }
    return changed;
  }

  inline static constexpr uint8_t BLANK_PIXEL[2] = {0, 0};
  uint8_t *front_{nullptr};
  uint32_t commit_count_{0};
  size_t last_copy_bytes_{0};
  std::array<DirtyRegion, DIRTY_REGION_COUNT> dirty_regions_{};
  CanvasFreshness freshness_{};
  CanvasFreshness pressed_freshness_{};
  CanvasRequestFeedback request_feedback_{};
};

inline StableCanvasFrame &canvas_frame() {
  static StableCanvasFrame frame;
  return frame;
}

}  // namespace solar_display
