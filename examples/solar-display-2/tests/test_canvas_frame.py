"""Compile the production display freshness code on a host without ESPHome."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1]
COMPILER = shutil.which("g++")

# The image and allocator methods only need declarations since this test exercises the clock.
IMAGE_DECLARATIONS = """
#pragma once
#include <cstddef>
#include <cstdint>
namespace esphome::image {
enum ImageType { IMAGE_TYPE_RGB565 };
enum Transparency { TRANSPARENCY_OPAQUE };
struct Pixel { int r, g, b; };
class Image {
 public:
  Image(const uint8_t *, int, int, ImageType, Transparency);
  Pixel get_pixel(int, int) const;
  int get_width() const;
  int get_height() const;
  ImageType get_type() const;
  bool has_transparency() const;
  const uint8_t *get_data_start() const;
  std::size_t get_width_stride() const;
 protected:
  const uint8_t *data_start_;
  int width_;
  int height_;
};
}
"""

ALLOCATOR_DECLARATIONS = """
#pragma once
#include <cstddef>
namespace esphome {
template<class T> class RAMAllocator {
 public:
  enum Allocation { ALLOC_EXTERNAL };
  explicit RAMAllocator(Allocation);
  T *allocate(std::size_t);
  void deallocate(T *, std::size_t);
};
}
"""


class CanvasFrameTest(unittest.TestCase):
    @unittest.skipUnless(COMPILER, "g++ with C++20 support is required")
    def test_frame_freshness(self):
        with tempfile.TemporaryDirectory(prefix="solar-display-canvas-") as scratch:
            directory = Path(scratch)
            for relative, source in (
                ("esphome/components/image/image.h", IMAGE_DECLARATIONS),
                ("esphome/core/helpers.h", ALLOCATOR_DECLARATIONS),
            ):
                header = directory / relative
                header.parent.mkdir(parents=True)
                header.write_text(source)
            binary = directory / "canvas_frame_test"
            subprocess.run(
                [
                    COMPILER,
                    "-std=c++20",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(EXAMPLE),
                    "-I",
                    str(directory),
                    str(EXAMPLE / "tests/canvas_frame_test.cpp"),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            subprocess.run([binary], check=True)


if __name__ == "__main__":
    unittest.main()
