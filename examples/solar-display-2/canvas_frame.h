#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>

#include "esphome/components/image/image.h"
#include "esphome/core/helpers.h"

#ifdef USE_LVGL
#include <src/misc/cache/instance/lv_image_cache.h>
#endif

namespace solar_display {

enum class ChargerButton { NONE, START, STOP };

// A deliberate tap starts and ends inside the same charger button.
inline ChargerButton charger_button_at(int x, int y) {
  if (y < 366 || y >= 428) return ChargerButton::NONE;
  if (x >= 20 && x < 232) return ChargerButton::START;
  if (x >= 248 && x < 460) return ChargerButton::STOP;
  return ChargerButton::NONE;
}

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
    if (source.get_width() != WIDTH || source.get_height() != HEIGHT ||
        source.get_type() != esphome::image::IMAGE_TYPE_RGB565 || source.has_transparency() ||
        source.get_data_start() == nullptr || source.get_width_stride() != static_cast<size_t>(WIDTH) * 2) {
      return CommitResult::INVALID;
    }

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

  // Decode the control data carried by this completed lossless image.
  std::string control_marker() const {
    if (!this->ready()) return {};
    constexpr char hex[]{"0123456789abcdef"};
    std::string marker;
    int cells = 69;
    for (int i = 0; i < cells; ++i) {
      const auto pixel = this->get_pixel(23 + 6 * i, 477);
      const int r = (pixel.r >> 6);
      const int g = (pixel.g >> 6);
      // The renderer resamples the PNG. Allow ringing around each cell's colour.
      if (pixel.b < 40 || pixel.b > 74 ||
          pixel.r < 8 + r * 64 || pixel.r > 56 + r * 64 ||
          pixel.g < 8 + g * 64 || pixel.g > 56 + g * 64) return {};
      marker += hex[4 * r + g];
      if (i == 3 && marker != "d519") return {};
      if (i == 4) {
        if (marker == "d5191") cells = 19;
        else if (marker != "d5192") return {};
      }
    }
    return (cells == 19 ? "start:" : "stop:") + marker.substr(5);
  }

  bool ready() const { return this->front_ != nullptr; }
  uint32_t commit_count() const { return this->commit_count_; }
  size_t last_copy_bytes() const { return this->last_copy_bytes_; }
  const std::array<DirtyRegion, DIRTY_REGION_COUNT> &dirty_regions() const { return this->dirty_regions_; }

 protected:
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
};

inline StableCanvasFrame &canvas_frame() {
  static StableCanvasFrame frame;
  return frame;
}

}  // namespace solar_display
