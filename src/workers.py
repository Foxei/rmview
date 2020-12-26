from PyQt5.QtGui import *
from PyQt5.QtCore import *

from rmparams import *

import struct
import time

import logging
log = logging.getLogger('rmview')

from lz4framed import Decompressor, Lz4FramedNoDataError

try:
  GRAY16 = QImage.Format_Grayscale16
  GRAY8 = QImage.Format_Grayscale8
except Exception:
  GRAY16 = QImage.Format_RGB16
RGB16 = QImage.Format_RGB16


SHOW_FPS = False

class FBWSignals(QObject):
  onFatalError = pyqtSignal(Exception)
  onNewFrame = pyqtSignal(QImage)


class FrameBufferWorker(QRunnable):

  _stop = False

  def __init__(self, ssh, delay=None, lz4_path=None, rmhead_path=None, img_format=GRAY8):
    super(FrameBufferWorker, self).__init__()
    #"dd if=/proc/$pid/mem bs=$page_size skip=$window_start_blocks count=$window_length_blocks 2>/dev/null | tail -c+$window_offset | reMarkable-test -c $window_bytes"

    self.ssh = ssh
    self.pid = self.get_process_id()
    self.skip_bytes = self.get_bytes_to_skip()
    self.window_start_blocks = self.get_window_start_block()
    self.window_offset = self.get_window_offset()
    self.window_length_blocks = self.get_window_length_blocks()

    self.img_format = img_format
    self.signals = FBWSignals()

    self._read_loop = """\
     while dd if=/proc/{pid}/mem bs={page_size} skip={window_start_blocks} count={window_length_blocks} 2>/dev/null | tail -c+{window_offset} | {rmhead_path} -c {window_bytes}; do true; done | {lz4_path}\
     """.format(pid=self.pid,
                page_size=PAGE_SIZE,
                window_offset=self.window_offset,
                window_start_blocks=self.window_start_blocks,
                window_length_blocks=self.window_length_blocks,
                window_bytes=WINDOW_BYTES,
                delay="sleep " + str(delay) if delay else "true",
                lz4_path=lz4_path or "$HOME/lz4",
                rmhead_path=rmhead_path or "$HOME/rmhead")

  def get_process_id(self):
    command = "pidof xochitl"
    log.info("Execution ssh command: %s", command)

    _, rmout, rmerr = self.ssh.exec_command("pidof xochitl")
    pid = int(rmout.read().decode('utf-8'))
    log.info("Process id extracted: %i", pid)

    return pid

  def get_bytes_to_skip(self):
    command = "grep -C1 '/dev/fb0' /proc/{pid}/maps | tail -n1 | sed 's/-.*$//'".format(pid=self.pid)
    log.info("Execution ssh command: %s", command)
    _, rmout, rmerr = self.ssh.exec_command(command)

    mem_location_string = "0x{bytes}".format(bytes=rmout.read().decode('utf-8'))
    mem_location_int = int(mem_location_string, 16)+8
    log.info("Memory location extracted: %s", mem_location_string)
    log.info("Bytes to skip: %i", mem_location_int)
    return mem_location_int

  def get_window_start_block(self):
    return int(self.skip_bytes/PAGE_SIZE)

  def get_window_offset(self):
    return int(self.skip_bytes % PAGE_SIZE)

  def get_window_length_blocks(self):
    return int(WINDOW_BYTES / PAGE_SIZE + 1)

  def stop(self):
    self._stop = True

  @pyqtSlot()
  def run(self):

    log.info("Execution ssh command: %s", self._read_loop)
    _, rmstream, rmerr = self.ssh.exec_command(self._read_loop)

    data = b''
    if SHOW_FPS:
      f = 0
      t = time.perf_counter()
      fps = 0

    try:
      for chunk in Decompressor(rmstream):
        data += chunk
        while len(data) >= WINDOW_BYTES:
          pix = data[:WINDOW_BYTES]
          data = data[WINDOW_BYTES:]
          self.signals.onNewFrame.emit(QImage(pix, WIDTH, HEIGHT, WIDTH, self.img_format))
          if SHOW_FPS:
            f += 1
            if f % 10 == 0:
              fps = 10 / (time.perf_counter() - t)
              t = time.perf_counter()
            print("FRAME %d  |  FPS %.3f\r" % (f, fps), end='')
        if self._stop:
          log.debug('Stopping framebuffer worker')
          break
    except Lz4FramedNoDataError:
      e = rmerr.read().decode('ascii')
      s = rmstream.channel.recv_exit_status()
      if s == 127:
        log.info("Check if your remarkable has lz4 installed! %s", e)
        self.signals.onFatalError.emit(Exception(e))
      else:
        log.warning("Frame data stream is empty.\nExit status: %d %s", s, e)

    except Exception as e:
      log.error("Error: %s %s", type(e), e)
      self.signals.onFatalError.emit(e)



class PWSignals(QObject):
  onFatalError = pyqtSignal(Exception)
  onPenMove = pyqtSignal(int, int)
  onPenPress = pyqtSignal()
  onPenLift = pyqtSignal()
  onPenNear = pyqtSignal()
  onPenFar = pyqtSignal()

LIFTED = 0
PRESSED = 1


class PointerWorker(QRunnable):

  _stop = False

  def __init__(self, ssh, threshold=1000):
    super(PointerWorker, self).__init__()
    self.ssh = ssh
    self.threshold = threshold
    self.signals = PWSignals()

  def stop(self):
    self._penkill.write('\n')
    self._stop = True

  @pyqtSlot()
  def run(self):
    penkill, penstream, _ = self.ssh.exec_command('cat /dev/input/event0 & { read ; kill %1; }')
    self._penkill = penkill
    new_x = new_y = False
    state = LIFTED

    while not self._stop:
      try:
        _, _, e_type, e_code, e_value = struct.unpack('2IHHi', penstream.read(16))
      except struct.error:
        return
      except Exception as e:
        log.error('Error in pointer worker: %s %s', type(e), e)
        return

      # decoding adapted from remarkable_mouse
      if e_type == e_type_abs:


        # handle x direction
        if e_code == e_code_stylus_xpos:
          x = e_value
          new_x = True

        # handle y direction
        if e_code == e_code_stylus_ypos:
          y = e_value
          new_y = True

        # handle draw
        if e_code == e_code_stylus_pressure:
          if e_value > self.threshold:
            if state == LIFTED:
              log.debug('PRESS')
              state = PRESSED
              self.signals.onPenPress.emit()
          else:
            if state == PRESSED:
              log.debug('RELEASE')
              state = LIFTED
              self.signals.onPenLift.emit()

        if new_x and new_y:
          self.signals.onPenMove.emit(x, y)
          new_x = new_y = False

      if e_type == e_type_key and e_code == e_code_stylus_proximity:
        if e_value == 0:
          self.signals.onPenFar.emit()
        else:
          self.signals.onPenNear.emit()



