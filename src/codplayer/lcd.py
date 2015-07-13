# codplayer - LCD display interfaces
#
# Copyright 2013-2015 Peter Liljenberg <peter.liljenberg@gmail.com>
#
# Distributed under an MIT license, please see LICENSE in the top dir.

#
# Interfaces used for the configuration objects
#

import sys
import time
from string import maketrans

from . import zerohub
from .state import State, RipState, StateClient
from . import command
from .codaemon import Daemon, DaemonError
from . import full_version

#
# Config object interfaces
#

class ILCDFactory(object):
    """Interface for LCD/LED controller factory classes.
    """
    def get_lcd_controller(self, columns, lines):
        """Create a LCD controller."""
        raise NotImplementedError()

    def get_led_controller(self):
        """Create a LED controller."""
        raise NotImplementedError()

    def get_text_encoder(self):
        """Return a function that encodes a string into
        another string that can be passed to
        lcd_controller.message().
        """
        raise NotImplementedError()


class ILCDFormatter(object):
    """Interface for LCD formatter classes.

    Each class formats for a particular screen size, defined by
    COLUMNS and LINES.
    """

    COLUMNS = None
    LINES = None

    def format(self, state, rip_state, disc, now):
        """Format the current player state into a string
        that can be sent to the LCD display.

        Each line should be separated by a newline character,
        but there should not be any trailing newline.

        It is up to the LCD controller class to do any optimisations
        to reduce the amount of screen that is redrawn, if applicable.

        The method shall return a (string, time_t) pair.  The time_t
        indicates how long the display string is valid, and requests
        that format() should be called again at this time even if no
        state have changed.  If the time_t is None, then the display
        string should not be updated until the next state change.

        state and rip_state can only be null at startup before the
        state have been received from the player.

        If disc is non-null, it corresponds to the disc currently
        loaded so the formatter does not need to check that.

        now is the current time.time(), as considered by the LCD
        display.  The formatter should use this rather than
        time.time() directly to determine if the display should
        change.
        """
        raise NotImplementedError()


#
# LCD (and LED) main daemon
#

class LCD(Daemon):
    # LED blink duration on button presses
    BUTTON_BLINK = 0.2

    def __init__(self, cfg, mq_cfg, debug = False):
        self._cfg = cfg
        self._mq_cfg = mq_cfg

        self._state = None
        self._rip_state = None
        self._disc = None

        self._led_selector = LEDPatternSelector()
        self._led_pattern = None
        self._led_generator = None
        self._led_timeout = None

        # Kick off deamon
        super(LCD, self).__init__(cfg, debug = debug)


    def setup_postfork(self):
        self._lcd_controller = self._cfg.lcd_factory.get_lcd_controller(
            self._cfg.formatter.COLUMNS, self._cfg.formatter.LINES)
        self._led_controller = self._cfg.lcd_factory.get_led_controller()
        self._text_encoder = self._cfg.lcd_factory.get_text_encoder()

        self._io_loop = zerohub.IOLoop.instance()


    def run(self):
        try:
            # Set up initial message
            self._lcd_controller.clear()
            self._lcd_update()

            self._led_update()

            # Set up subscriptions on relevant state updates
            state_receiver = StateClient(
                channel = self._mq_cfg.state,
                io_loop = self._io_loop,
                on_state = self._on_state,
                on_rip_state = self._on_rip_state,
                on_disc = self._on_disc
            )

            # Blink LED on button presses
            button_receiver = zerohub.Receiver(
                self._mq_cfg.input, name = 'codlcd', io_loop = self._io_loop,
                callbacks = { 'button.': self._on_button_press })

            # Kickstart things by requesting the current state from the player
            rpc_client = command.AsyncCommandRPCClient(
                zerohub.AsyncRPCClient(
                    channel = self._mq_cfg.player_rpc,
                    name = 'codlcd',
                    io_loop = self._io_loop))

            rpc_client.call('source', on_response = self._on_disc)
            rpc_client.call('state', on_response = self._on_state)
            rpc_client.call('rip_state', on_response = self._on_rip_state)

            # Let the IO loop take care of the rest
            self._io_loop.start()

        finally:
            # Turn off LED and clear display on exit to tell user that
            # there's no LED/LCD control anymore
            self._lcd_controller.clear()
            self._led_controller.off()


    def _on_state(self, state):
        self.debug('got state: {}', self._state)

        self._state = state
        self._lcd_update()
        self._led_update()


    def _on_rip_state(self, rip_state):
        self.debug('got rip state: {}', self._rip_state)

        self._rip_state = rip_state
        self._lcd_update()
        self._led_update()


    def _on_disc(self, disc):
        self.debug('got disc: {}', self._disc)

        self._disc = disc
        self._lcd_update()


    def _on_button_press(self, receiver, msg):
        if self._led_pattern is None:
            # Only blink on button press when there's no pattern blinking

            if self._led_timeout is not None:
                # LED is off due to button blink, so light it immediately to
                # flicker LED with button repeats
                self._led_controller.on()
                self._io_loop.remove_timeout(self._led_timeout)
                self._led_timeout = None
            else:
                # LED is on and no timeout, so start a new one
                self._led_timeout = self._io_loop.add_timeout(time.time() + self.BUTTON_BLINK, self._stop_button_blink)
                self._led_controller.off()


    def _lcd_update(self):
        # Only pass the formatter a disc object relevant to the state
        if self._state and self._disc and self._state.disc_id == self._disc.disc_id:
            disc = self._disc
        else:
            disc = None

        now = time.time()
        msg, timeout = self._cfg.formatter.format(self._state, self._rip_state, disc, now)
        if timeout is not None:
            self._io_loop.add_timeout(timeout, self._lcd_update)

        self._lcd_controller.home()
        self._lcd_controller.message(self._text_encoder(msg))


    def _led_update(self):
        pattern = self._led_selector.get_pattern(self._state, self._rip_state)
        if pattern is not self._led_pattern:

            self._led_pattern = pattern

            # Stop any running blink pattern
            self._led_generator = None
            if self._led_timeout is not None:
                self._io_loop.remove_timeout(self._led_timeout)
                self._led_timeout = None

            # Set solid light or start new pattern
            if pattern is None:
                self._led_controller.on()
            else:
                self._led_generator = pattern.generate(time.time())
                self._blink_led()


    def _blink_led(self):
        value, timeout = self._led_generator.next()
        self._led_controller.set(value)
        self._led_timeout = self._io_loop.add_timeout(timeout, self._blink_led)


    def _stop_button_blink(self):
        if self._led_pattern is None:
            self._led_timeout = None
            self._led_controller.on()

#
# LCD Formatters
#

class LCDFormatterBase(ILCDFormatter):
    """Base class for LCD formatters of different sizes."""

    COLUMNS = None
    LINES = None

    # Custom CGRAM positions
    PLAY = '\x00'
    PAUSE = '\x01'

    UNKNOWN_STATE = '?'

    # Seconds between each scroll update
    SCROLL_SPEED = 0.3

    # Seconds scrolled text remains at end position
    SCROLL_PAUSE = 1

    def __init__(self):
        self._state = None
        self._rip_state = None
        self._disc = None
        self._current_track_number = 0
        self._info_generator = None
        self._info_lines = ''
        self._next_info_lines = None
        self._errors = None


    def fill(self, *lines):
        """Return a set of lines filled out to the full width and height of
        the display as a newline-separated string.
        """
        output_lines = []
        for line in lines:
            line = line[:self.COLUMNS]
            line = line + ' ' * (self.COLUMNS - len(line))
            output_lines.append(line)

        while len(output_lines) < self.LINES:
            output_lines.append(' ' * self.COLUMNS)

        return '\n'.join(output_lines[:self.LINES])


    def scroll(self, text, now, prefix = '', columns = None, loop = False):
        """Iterator that scrolls through text if necessary to fit
        within the line length.
        """

        if columns is None:
            columns = self.COLUMNS

        columns -= len(prefix)

        if len(text) <= columns:
            # No need to scroll
            yield prefix + text, None

        else:
            while True:
                # Show first part a little longer
                now += self.SCROLL_PAUSE
                yield prefix + text[:columns], now

                # Then start scrolling
                for i in range(1, len(text) - columns):
                    now += self.SCROLL_SPEED
                    yield prefix + text[i : i + columns], now

                # Show last position a little longer
                now += self.SCROLL_PAUSE
                yield prefix + text[-columns:], now

                if not loop:
                    # Switch back to first position and scroll no more
                    yield prefix + text[:columns], None
                    return


    def format(self, state, rip_state, disc, now):
        if state is None:
            return self.fill(
                full_version(),
                'Waiting on state'), None

        self._state = state
        self._rip_state = rip_state

        # Check if there are error messages
        errors = None
        if state and state.error:
            errors = [state.error]
        if rip_state and rip_state.error:
            if errors:
                errors.append(rip_state.error)
            else:
                errors = [rip_state.error]

        if errors:
            if self._errors != errors:
                self._errors = errors
                # Need new formatter
                self._info_generator = self.generate_error_lines(now)
                self._next_info_lines = now

        else:
            # No current errors, show disc info

            # Show new disc info lines when disc change, or
            # when coming back from error
            if self._disc is not disc or self._errors:
                self._errors = None
                if disc is None:
                    self._disc = None
                    self._current_track_number = 0
                    self._info_generator = None
                    self._next_info_lines = None
                    self._info_lines = ''
                else:
                    self._disc = disc
                    self._current_track_number = state.track
                    self._info_generator = self.generate_disc_lines(now)
                    self._next_info_lines = now

            # Show new track info when switching track on a disc
            elif self._disc and self._current_track_number != state.track:
                self._current_track_number = state.track
                self._info_generator = self.generate_track_lines(now)
                self._next_info_lines = now

        # Update info line if there is still a generator
        # and the time has come
        while (self._info_generator is not None and
               self._next_info_lines is not None and
               now >= self._next_info_lines):
            try:
                self._info_lines, self._next_info_lines = self._info_generator.next()
            except StopIteration:
                # Keep the current info and stop generating new lines
                self._info_generator = None
                self._next_info_lines = None

        return self.do_format(), self._next_info_lines

    def do_format(self):
        raise NotImplementedError()

    def generate_disc_lines(self, now):
        """Return an iterator that provides information for a new disc.
        The generated values should be a tuple: (lines, next_update)

        This is not called if self._errors is non-empty.

        next_update is the unix time when the next value should be
        shown.  The final generated value should have next_update = None
        """
        raise NotImplementedError()

    def generate_track_lines(self, now):
        """Return an iterator that provides information for a new track.
        Behaves the same way as generate_disc_lines().

        This is not called if self._errors is non-empty.
        """
        raise NotImplementedError()

    def generate_error_lines(self, now):
        """Return an iterator that provides information for errors.
        Behaves the same way as generate_disc_lines().

        This is only called if self._errors is non-empty.
        """
        raise NotImplementedError()


class LCDFormatter16x2(LCDFormatterBase):
    """Format output for a 16x2 LCD display.

    See the TestLCDFormatter16x2 unit test for the expected
    output of this class for different player states.
    """

    COLUMNS = 16
    LINES = 2

    # Seconds that disc title and artist is kept visible
    DISC_INFO_SWITCH_SPEED = 3


    def scroll(self, text, now, prefix = '', loop = False):
        """Override to fit info line scroll into available space, which is
        shortened when ripping is in progress and should be shown at
        the end of the line.

        If ripping ends during the track this extra space will not be
        reclaimed until the next track starts.
        """

        scroll_columns = self.COLUMNS
        if self._rip_state and self._rip_state.state != RipState.INACTIVE:
            scroll_columns -= 4

        return super(LCDFormatter16x2, self).scroll(
            text, now, prefix = prefix, columns = scroll_columns, loop = loop)


    def do_format(self):
        return self.fill(
            self.do_format_state_line(),
            self.do_format_info_line())

    def do_format_state_line(self):
        s = self._state

        if s.state is State.OFF:
            return 'Player shut down'

        if s.state is State.NO_DISC:
            return 'No disc'

        elif s.state is State.STOP:
            return 'Stop {0.no_tracks:>4d} tracks'.format(s)

        elif s.state is State.WORKING:
            return 'Working {0.track:d}/{0.no_tracks:d}...'.format(s)

        else:
            if s.state is State.PLAY:
                state_char = self.PLAY
            elif s.state is State.PAUSE:
                state_char = self.PAUSE
            else:
                state_char = self.UNKNOWN_STATE

            if s.position < 0:
                # pregap
                track_pos = '-{0:d}:{1:02d}'.format(
                    abs(s.position) / 60, abs(s.position) % 60)
            else:
                # Regular position
                track_pos = ' {0:d}:{1:02d}'.format(
                    s.position / 60, s.position % 60)

            if s.length >= 600 and (s.track >= 10 or
                    (s.position >= 600 and s.no_tracks >= 10)):
                # Can only fit rough track length
                track_len = '{0:d}+'.format(s.length / 60)
            else:
                # Full track length will fit
                track_len = '{0:d}:{1:02d}'.format(
                    s.length / 60, s.length % 60)

            if s.length < 600:
                # Can give space to track numbers at left
                state_line = '{0}{1.track:>2d}/{1.no_tracks:<2d}{2}/{3}'.format(
                    state_char, s, track_pos, track_len)
            else:
                # Need to shift track numbers far to the left
                tracks = '{0}{1.track:d}/{1.no_tracks:d}'.format(state_char, s)
                pos = '{0}/{1}'.format(track_pos, track_len)
                fill = 16 - len(tracks) - len(pos)
                state_line = tracks + (' ' * fill) + pos

            return state_line


    def do_format_info_line(self):
        s = self._rip_state

        if s and s.state is RipState.AUDIO:
            if s.progress is not None:
                return '{0:<12s}{1:3d}%'.format(self._info_lines[:12], s.progress)
            else:
                return '{0:<12s} RIP'.format(self._info_lines[:12])
        elif s and s.state == RipState.TOC:
            return '{0:<12s} TOC'.format(self._info_lines[:12])
        else:
            return self._info_lines


    def generate_disc_lines(self, now):
        """Show album title and artist in sequence, then switch to track title.
        All are scrolled if necessary
        """

        disc_title = self._disc.title or 'Unknown album'
        for line, next_update in self.scroll(disc_title, now):
            if next_update is None:
                # Pause until switching to disc artist
                now += self.DISC_INFO_SWITCH_SPEED
                yield line, now
            else:
                now = next_update
                yield line, next_update

        disc_artist = self._disc.artist or 'Unknown artist'
        for line, next_update in self.scroll(disc_artist, now):
            if next_update is None:
                # Pause until switching to track title
                now += self.DISC_INFO_SWITCH_SPEED
                yield line, now
            else:
                now = next_update
                yield line, next_update

        for line, next_update in self.generate_track_lines(now):
            yield line, next_update


    def generate_track_lines(self, now):
        """Show track title, scrolled initially.
        """

        if self._state.track == 0:
            return self.scroll(self._disc.title or 'Unknown album', now)

        else:
            track_index = self._state.track - 1
            if track_index >= len(self._disc.tracks):
                return iter([('Bad track list!', None)])
            else:
                track = self._disc.tracks[track_index]
                track_title = track.title or 'Unknown track'
                if track.number != self._state.track:
                    # Track numbering is off, so show actual number here
                    prefix = '{}. '.format(track.number)
                else:
                    prefix = ''
                return self.scroll(track_title, now, prefix)

    def generate_error_lines(self, now):
        return self.scroll('; '.join(self._errors), now, loop = True)


#
# LED blinkenlichts
#

class LEDState(object):
    value = None

    def __init__(self, duration):
        self.duration = duration

class ON(LEDState):
    value = 1

class OFF(LEDState):
    value = 0

class BlinkPattern(object):
    """Define a LED blink pattern of alternating ON and OFF states.
    """
    def __init__(self, *states):
        self.states = states

    def generate(self, now):
        """Return a iterator that generates the pattern.

        Each generated value is a tuple (value, next_state) where
        value is a value between 0 and 1 (typically either 1 or 0),
        and next_state is the time_t when this value should be
        replaced by the next one.
        """

        while True:
            for state in self.states:
                now += state.duration
                yield state.value, now


class LEDPatternSelector(object):

    NO_PLAYER = BlinkPattern(ON(0.3), OFF(0.9))

    WORKING = BlinkPattern(OFF(0.3), ON(0.3))

    PLAYER_ERROR = BlinkPattern(OFF(0.9), ON(0.3), OFF(0.3), ON(0.3))

    def get_pattern(self, state, rip_state):
        """Return a BlinkPattern object for the current
        state and rip_state.  If the returned object
        is the same as the currently processed pattern, it
        will not be restarted.

        Return None to stop the current pattern and return
        the LED to the default state.
        """

        if rip_state:
            if rip_state.error:
                return self.PLAYER_ERROR

        if state:
            if state.state is State.OFF:
                return self.NO_PLAYER

            elif state.error:
                return self.PLAYER_ERROR

            elif state.state is State.WORKING:
                return self.WORKING
        else:
            return self.NO_PLAYER

        return None


#
# RPi GPIO interface
#

class GPIO_LCDFactory(ILCDFactory):
    PLAY_CHAR = (0x10,0x18,0x1c,0x1e,0x1c,0x18,0x10,0x0)
    PAUSE_CHAR = (0x0,0x1b,0x1b,0x1b,0x1b,0x1b,0x0,0x0)

    def __init__(self, led,
                 rs, en, d4, d5, d6, d7,
                 backlight = None,
                 custom_chars = None):
        """Create an LCD and LED controller using RPi GPIO pins
        (using AdaFruit libraries).

        led, rs, en, d4, d5, d6, d7, backlight are BCM pin numbers, passed
        to the Adafruit_CharLCD interface.

        custom_chars can be a mapping of up to six characters that
        should be defined as custom CGRAMs in the LCD, in case its ROM
        lack some important characters for you.  The key is a
        single-char string, the value is a tuple of eight byte values
        defining the bit pattern.  These can be created using
        http://www.quinapalus.com/hd44780udg.html
        """

        self._pin_led = led
        self._pin_rs = rs
        self._pin_en = en
        self._pin_d4 = d4
        self._pin_d5 = d5
        self._pin_d6 = d6
        self._pin_d7 = d7
        self._pin_backlight = backlight

        # First two CGRAM positions are already taken
        self._custom_char_patterns = [self.PLAY_CHAR, self.PAUSE_CHAR]

        # Remaining can be defined by user
        if custom_chars:
            assert len(custom_chars) <= 6

            i = 2
            trans_from = ''
            trans_to = ''
            for char, pattern in custom_chars.items():
                assert len(char) == 1
                assert len(pattern) == 8

                self._custom_char_patterns.append(pattern)
                trans_from += char
                trans_to += chr(i)
                i += 1

            self._custom_trans = maketrans(trans_from, trans_to)
        else:
            self._custom_trans = None


    def get_lcd_controller(self, columns, lines):
        from Adafruit_CharLCD import Adafruit_CharLCD
        lcd = Adafruit_CharLCD(
            cols = columns,
            lines = lines,
            rs = self._pin_rs,
            en = self._pin_en,
            d4 = self._pin_d4,
            d5 = self._pin_d5,
            d6 = self._pin_d6,
            d7 = self._pin_d7,
            backlight = self._pin_backlight,
            invert_polarity = False,
        )

        # Create custom chars, since not all ROMS have the PLAY and
        # PAUSE chars or other ones the user might need
        for i, pattern in enumerate(self._custom_char_patterns):
            lcd.create_char(i, pattern)

        return lcd

    def get_led_controller(self):
        import Adafruit_GPIO as GPIO
        return GPIO_LEDController(GPIO.get_platform_gpio(), self._pin_led)


    def get_text_encoder(self):
        return self._encode

    def _encode(self, text):
        # TODO: this could be much more advanced, but for now squish into iso-8859-1
        # (which maps decently to an HD44780 ROM type 02)

        text = text.encode('iso-8859-1')

        # Apply any custom translations.  OK now that this is no longer unicode.
        if self._custom_trans:
            text = text.translate(self._custom_trans)

        return text


class GPIO_LEDController(object):
    def __init__(self, gpio, pin):
        import Adafruit_GPIO as GPIO

        self._gpio = gpio
        self._pin = pin

        # Setup for output
        gpio.setup(pin, GPIO.OUT)
        self.off()

    def on(self):
        self._gpio.output(self._pin, 1)

    def off(self):
        self._gpio.output(self._pin, 0)

    def set(self, value):
        self._gpio.output(self._pin, value)


#
# Test classes for developing without the HW
#

class TestLCDFactory(ILCDFactory):
    def __init__(self):
        self._led_controller = TestLEDController()

    def get_lcd_controller(self, columns, lines):
        return TestLCDController(columns, lines, self._led_controller)

    def get_led_controller(self):
        return self._led_controller

    def get_text_encoder(self):
        # No-op function
        return lambda text: text


class TestLCDController(object):
    """Use VT100 escape sequences to draw a LCD display on the second line
    and down.
    """
    def __init__(self, columns, lines, led_controller):
        self._columns = columns
        self._lines = lines
        self._led_controller = led_controller

    def home(self):
        # Redraw LED controller first since it may have scrolled off
        self._led_controller._redraw()

        sys.stdout.write('\x1B[s')    # save cursor
        sys.stdout.write('\x1B[2;1H') # go to row 2, col 1
        sys.stdout.flush()

    def clear(self):
        sys.stdout.write('\x1Bc') # clear screen
        sys.stdout.write('\x1B[10;1H') # go to row 10, col 1
        sys.stdout.flush()

    def message(self, text):
        text = text.replace(LCDFormatterBase.PLAY, '>')
        text = text.replace(LCDFormatterBase.PAUSE, '=')

        header = '+' + '-' * self._columns + '+'
        sys.stdout.write('\x1B[K')    # clear until end of line
        sys.stdout.write(header)

        for line in text.split('\n'):
            sys.stdout.write('\x1B[B\r')    # down one line, first position
            sys.stdout.write('\x1B[K')    # clear until end of line
            sys.stdout.write('|' + line + '|')

        sys.stdout.write('\x1B[B\r')    # down one line, first position
        sys.stdout.write('\x1B[K')    # clear until end of line
        sys.stdout.write(header)

        sys.stdout.write('\x1B[u')    # unsave cursor
        sys.stdout.flush()

    def set_backlight(self, backlight):
        pass


class TestLEDController(object):
    def __init__(self):
        self._state = 0

    def on(self):
        self._state = 1
        self._redraw()

    def off(self):
        self._state = 0
        self._redraw()

    def set(self, value):
        assert value in (0, 1)
        if value:
            self.on()
        else:
            self.off()

    def _redraw(self):
        sys.stdout.write('\x1B[s')    # save cursor
        sys.stdout.write('\x1B[1;1H') # go to row 1, col 1
        sys.stdout.write('\x1B[K')    # clear until end of line

        if self._state:
            sys.stdout.write('LED: ***     ')
        else:
            sys.stdout.write('LED:  .      ')

        sys.stdout.write('\x1B[u')    # unsave cursor
        sys.stdout.flush()

