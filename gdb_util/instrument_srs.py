# code to instrument std::sort for my custom type
# see examples/sort_random_sequence.cpp

import gdb
import tempfile
import os
from threading import Thread
from queue import Queue

# animated display of operation
from PyQt5.QtWidgets import QWidget, QApplication, QMainWindow
from PyQt5.QtCore import Qt, QTimer

class SwapAnimation(QMainWindow):
    def __init__(self):
        super(self.__class__, self).__init__()

        self.setStyleSheet('background-color: yellow')
        self.timer = QTimer()
        self.timer.timeout.connect(self._timeout)
        self.timer.start(1000)

    def _timeout(self):
        self.setStyleSheet('background-color: brown')


class GuiThread(Thread):
    def __init__(self, base_addr, size):
        Thread.__init__(self)
        self.base_addr = base_addr  # the vector we are monitoring
        self.size = size            # its size
        self.messages = Queue()     # cross-thread communication
        # debug print contents of vec
        int_t = gdb.lookup_type('int')
        for idx in range(0, size):
            print('idx %d value %d'%(idx, (base_addr + idx).dereference().cast(int_t)))


    # next, updates for instrumented actions
    def show_swap(self, a, b):
        # sending gdb.Value objects over the queue doesn't seem to work
        # at least, their addresses are no longer accessible in the other thread
        # So we'll do the calculations here
        a_idx = a.address - self.base_addr
        b_idx = b.address - self.base_addr
        self._send_message('swap', int(a_idx), int(b_idx))

    def show_move(self, a, b):  # a moved into from b
        # a is always an address and b is an rvalue reference
        # so we use "a" and "b.address"

        # detect whether a or b is a temporary
        a_in_vec = (a >= self.base_addr) and (a < (self.base_addr + self.size))
        b_in_vec = (b.address >= self.base_addr) and (b.address < (self.base_addr + self.size))
        # print('a address = %s, b address = %s, base is %s, size is %s'%(a.address, b.address, self.base_addr, self.size))

        # we will supply temporaries as their address in string form,
        # and in-vector quantities as their offset (a Python int)
        # this way gdb.Value objects don't outlive their frame

        if a_in_vec and b_in_vec:
            a_idx = a - self.base_addr
            b_idx = b.address - self.base_addr
            self._send_message('move', int(a_idx), int(b_idx))
        elif a_in_vec:
            # source is a temporary; stringify its address to use as a token representing it
            a_idx = a - self.base_addr
            self._send_message('move_from_temp', str(b.address), int(a_idx))
        elif b_in_vec:
            # dest is a temporary
            b_idx = b.address - self.base_addr
            self._send_message('move_to_temp', int(b_idx), str(a))
        else:
            # I've never seen a move from temporary to temporary
            raise RuntimeError('saw an unexpected move from temporary to temporary')

    def _send_message(self, tp, src, dst):
        self.messages.put((tp, src, dst))   # contents are swap info

    def _check_for_messages(self):
        # poll command queue
        # not ideal but safe. OK for now.
        if not self.messages.empty():
            op, a, b = self.messages.get()
            if op is 'swap':
                # actually seems to understand the size of the elements:
                print('got command to swap offsets %s and %s'%(a, b))
            else:
                print('got move command from %s to %s'%(a, b))

    def run(self):
        # a warning message about not being run in the main thread gets printed
        # everything I can find suggests this is not a real issue, so long as
        # all QObject access happens in the same thread, which it is.
        self.app = QApplication([])

        # periodically poll command queue
        self.cmd_poll_timer = QTimer()
        self.cmd_poll_timer.timeout.connect(self._check_for_messages)
        self.cmd_poll_timer.start(100)   # 100ms doesn't seem too terrible *shrug*

        self.top = SwapAnimation()
        self.top.show()
        self.app.exec_()


#
# define observability breakpoints
#

# my special swap, initially disabled to avoid the call to std::shuffle
swap_bp = gdb.Breakpoint('swap(int_wrapper_t&, int_wrapper_t&)')
swap_bp.enabled = False # off until we get to our algorithm of interest
swap_bp.silent = True   # don't spam user

# move ctor
move_bp = gdb.Breakpoint('int_wrapper_t::int_wrapper_t(int_wrapper_t&&)')
move_bp.enabled = False
move_bp.silent = True

# move assignment operator
move_assign_bp = gdb.Breakpoint('int_wrapper_t::operator=(int_wrapper_t&&)')
move_assign_bp.enabled = False
move_assign_bp.silent = True

# and for the algorithm itself:
sort_bp = gdb.Breakpoint('std::sort<std::vector<int_wrapper_t, std::allocator<int_wrapper_t> >::iterator>')
sort_bp.enabled = True
sort_bp.silent = True

# next prepare to enable and execute the swap display commands

# breakpoint's commands property was made writable too recently for me to use:
# https://sourceware.org/bugzilla/show_bug.cgi?id=22731
# instead we have to write out a script to a tempfile... groan...
tf = tempfile.NamedTemporaryFile(mode='w', delete=False)

# actions for when we arrive at std::sort
# TODO is there a way to improve this formatting?
tf.write(("commands %d\n"
          # a breakpoint at the end of std::sort, for cleanup and to keep our process alive
          "py finish_bp = gdb.FinishBreakpoint()\n"
          # move up to the main() frame to accessvariables
          "py gdb.selected_frame().older().select()\n"
          # tell our gui thread about the container being sorted
          # new gdb 8.1.1 does not seem to understand the operator[], though 8.1.0 did
#          "py gdb_util.instrument_srs.gui = gdb_util.instrument_srs.GuiThread(gdb.parse_and_eval('&A[0]'), gdb.parse_and_eval('A.size()'))\n"
          "py gdb_util.instrument_srs.gui = gdb_util.instrument_srs.GuiThread(gdb.parse_and_eval('A._M_impl._M_start'), gdb.parse_and_eval('A._M_impl._M_finish - A._M_impl._M_start'))\n"
          # launch gui
          "py gdb_util.instrument_srs.gui.start()\n"
          # turn on observability breakpoints
          "enable %d\n"
          "enable %d\n"
          "enable %d\n"
          # run the algorithm
          "c\n"
          "end\n")%(sort_bp.number, swap_bp.number, move_bp.number, move_assign_bp.number))

# actions for each swap()
tf.write(("commands %d\n"
          "py gdb_util.instrument_srs.gui.show_swap(gdb.selected_frame().read_var('a'), gdb.selected_frame().read_var('b'))\n"
          "py gdb_util.instrument_srs.skip_through_swap(%d, %d)\n"
          # resume
          "c\n"
          "end\n")%(swap_bp.number, move_bp.number, move_assign_bp.number))

# actions for move (either construct or assign)
# Weird observation: these run without having to hit return at the prompt... can this lead to a workaround?
move_actions = ("commands %d\n"
                "py gdb_util.instrument_srs.gui.show_move(gdb.selected_frame().read_var('this'), gdb.selected_frame().read_var('other'))\n"
                "c\n"
                "end\n")
tf.write(move_actions%move_bp.number)
tf.write(move_actions%move_assign_bp.number)

tf.flush()
tf.close()
gdb.execute('source %s'%tf.name)
os.unlink(tf.name)

def skip_through_swap(mbp1, mbp2):
    # disable the move breakpoints (they will otherwise trigger during the swap)
    bps = gdb.breakpoints()
    bp1 = bps[mbp1-1]
    bp2 = bps[mbp2-1]
    bp1.enabled = False
    bp2.enabled = False
    # run to the end of swap
    fbp = gdb.FinishBreakpoint(internal = True)
    fbp.silent = True
    gdb.execute('c')
    # restore
    bp1.enabled = True
    bp2.enabled = True
    gdb.execute('continue')
