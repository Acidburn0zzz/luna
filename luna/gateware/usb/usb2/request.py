#
# This file is part of LUNA.
#
""" Low-level USB transciever gateware -- control request components. """

import unittest

from nmigen            import Signal, Module, Elaboratable, Cat, Record
from ...test           import usb_domain_test_case

from .                 import USBSpeed
from .packet           import USBTokenDetector, USBDataPacketDeserializer, USBPacketizerTest
from .packet           import DataCRCInterface, USBInterpacketTimer


class USBSetupDecoder(Elaboratable):
    """ Gateware responsible for detecting Setup transactions.

    I/O port:
        *: crc_interface  -- Interface to our data-CRC generator.
        I: speed          -- The device's current operating speed. Should be a USBSpeed
                             enumeration value -- 0 for high, 1 for full, 2 for low.

    """

    SETUP_PID = 0b1101

    def __init__(self, *, utmi, tokenizer, timer, own_tokenizer=False, standalone=False):
        """
        Paremeters:
            utmi           -- The UTMI bus we'll monitor for data. We'll consider this read-only.
            tokenizer      -- The USBTokenDetector detecting token packets for us. Considered read-only.

            standalone     -- Debug parameter. If true, this module will operate without external components;
                              i.e. without an internal data-CRC generator, or tokenizer. In this case, tokenizer
                              and timer should be set to None; and will be ignored.
        """
        self.utmi          = utmi
        self.timer         = timer
        self.tokenizer     = tokenizer
        self.standalone    = standalone

        #
        # Control/timing signals for our timer instance.
        #

        if self.standalone:
            self.timer = USBInterpacketTimer()

        # Interpacket timer reset; should be pulsed when we receive new data.
        self._start_interpacket_timer = Signal()
        self.timer.add_reset_condition(self._start_interpacket_timer)

        # Interpacket delay complete: pulses when it's safe to transmit our ACK.
        self._interpacket_delay_complete = self.timer.get_transmit_allowed_strobe()

        #
        # I/O port.
        #
        self.data_crc      = DataCRCInterface()
        self.speed         = Signal(2)

        self.new_packet    = Signal()
        self.ack           = Signal()

        self.is_in_request = Signal()
        self.type          = Signal(2)
        self.recipient     = Signal(5)

        self.request       = Signal(8)
        self.value         = Signal(16)
        self.index         = Signal(16)
        self.length        = Signal(16)


    def elaborate(self, platform):
        m = Module()

        # If we're standalone, generate the things we need.
        if self.standalone:

            # Create our tokenizer...
            self.tokenizer = USBTokenDetector(utmi=self.utmi)
            m.submodules.tokenizer = self.tokenizer

            # ... and hook up our timer.
            m.submodules.timer = self.timer
            m.d.comb += self.timer.speed.eq(self.speed)

        # Create a data-packet-deserializer, which we'll use to capture the
        # contents of the setup data packets.
        m.submodules.data_handler = data_handler = \
            USBDataPacketDeserializer(utmi=self.utmi, max_packet_size=8, create_crc_generator=self.standalone)
        m.d.comb += self.data_crc.connect(data_handler.data_crc)

        # Instruct our interpacket timer to begin counting when we complete receiving
        # our setup packet. This will allow us to track interpacket delays.
        m.d.comb += self._start_interpacket_timer.eq(data_handler.new_packet)

        # Keep our output signals de-asserted unless specified.
        m.d.usb += [
            self.new_packet  .eq(0),
        ]

        with m.FSM(domain="usb"):

            # IDLE -- we haven't yet detected a SETUP transaction directed at us
            with m.State('IDLE'):
                pid_matches     = (self.tokenizer.pid     == self.SETUP_PID)

                # If we're just received a new SETUP token addressed to us,
                # the next data packet is going to be for us.
                with m.If(pid_matches & self.tokenizer.new_token):
                    m.next = 'READ_DATA'


            # READ_DATA -- we've just seen a SETUP token, and are waiting for the
            # data payload of the transaction, which contains the setup packet.
            with m.State('READ_DATA'):

                # If we receive a token packet before we receive a DATA packet,
                # this is a PID mismatch. Bail out and start over.
                with m.If(self.tokenizer.new_token):
                    m.next = 'IDLE'

                # If we have a new packet, parse it as setup data.
                with m.If(data_handler.new_packet):

                    # If we got exactly eight bytes, this is a valid setup packet.
                    with m.If(data_handler.length == 8):

                        request_type = Cat(self.recipient, self.type, self.is_in_request)

                        m.d.usb += [

                            # Parse the setup data itself...
                            request_type     .eq(data_handler.packet[0]),
                            self.request     .eq(data_handler.packet[1]),
                            self.value       .eq(Cat(data_handler.packet[2], data_handler.packet[3])),
                            self.index       .eq(Cat(data_handler.packet[4], data_handler.packet[5])),
                            self.length      .eq(Cat(data_handler.packet[6], data_handler.packet[7])),

                            # ... and indicate that we have new data.
                            self.new_packet  .eq(1),

                        ]


                        # We'll now need to wait a receive-transmit delay before initiating our ACK.
                        # Per the USB 2.0 and ULPI 1.1 specifications:
                        #   - A HS device needs to wait 8 HS bit periods before transmitting [USB2, 7.1.18.2].
                        #     Each ULPI cycle is 8 HS bit periods, so we'll only need to wait one cycle.
                        #   - We'll use our interpacket delay timer for everything else.
                        with m.If(self.speed == USBSpeed.HIGH):

                            # If we're a high speed device, we only need to wait for a single ULPI cycle.
                            # Processing delays mean we've already met our interpacket delay; and we can ACK
                            # immediately.
                            m.d.comb += self.ack.eq(1)
                            m.next = "IDLE"

                        # For other cases, handle the interpacket delay by waiting.
                        with m.Else():
                            m.next = "INTERPACKET_DELAY"


                    # Otherwise, this isn't; and we should ignore it. [USB2, 8.5.3]
                    with m.Else():
                        m.next = "IDLE"


            # INTERPACKET -- wait for an inter-packet delay before responding
            with m.State('INTERPACKET_DELAY'):

                # ... and once it equals zero, ACK and return to idle.
                with m.If(self._interpacket_delay_complete):
                    m.d.comb += self.ack.eq(1)
                    m.next = "IDLE"

        return m


class USBSetupDecoderTest(USBPacketizerTest):

    def instantiate_dut(self):
        self.utmi = Record([
            ("rx_data",   8),
            ("rx_active", 1),
            ("rx_valid",  1)
        ])

        # ... and our setup detector, around it.
        return USBSetupDecoder(utmi=self.utmi, tokenizer=None, timer=None, standalone=True)


    def initialize_signals(self):

        # Assume high speed.
        yield self.dut.speed.eq(USBSpeed.HIGH)


    def provide_reference_setup_transaction(self):
        """ Provide a reference SETUP transaction. """

        # Provide our setup packet.
        yield from self.provide_packet(
            0b00101101, # PID: SETUP token.
            0b00000000, 0b00010000 # Address 0, endpoint 0, CRC
        )

        # Provide our data packet.
        yield from self.provide_packet(
            0b11000011,   # PID: DATA0
            0b0_10_00010, # out vendor request to endpoint
            12,           # request number 12
            0xcd, 0xab,   # value  0xABCD (little endian)
            0x23, 0x01,   # index  0x0123
            0x78, 0x56,   # length 0x5678
            0x3b, 0xa2,   # CRC
        )


    @usb_domain_test_case
    def test_valid_sequence_receive(self):
        dut = self.dut

        # Before we receive anything, we shouldn't have a new packet.
        self.assertEqual((yield dut.new_packet), 0)

        # Simulate the host sending basic setup data.
        yield from self.provide_reference_setup_transaction()

        # We're high speed, so we should be ACK'ing immediately.
        self.assertEqual((yield dut.ack), 1)

        # We now should have received a new setup request.
        yield
        self.assertEqual((yield dut.new_packet), 1)

        # Validate that its values are as we expect.
        self.assertEqual((yield dut.is_in_request), 0       )
        self.assertEqual((yield dut.type),          0b10    )
        self.assertEqual((yield dut.recipient),     0b00010 )
        self.assertEqual((yield dut.request),       12      )
        self.assertEqual((yield dut.value),         0xabcd  )
        self.assertEqual((yield dut.index),         0x0123  )
        self.assertEqual((yield dut.length),        0x5678  )


    @usb_domain_test_case
    def test_fs_interpacket_delay(self):
        dut = self.dut

        # Place our DUT into full speed mode.
        yield dut.speed.eq(USBSpeed.FULL)

        # Before we receive anything, we shouldn't have a new packet.
        self.assertEqual((yield dut.new_packet), 0)

        # Simulate the host sending basic setup data.
        yield from self.provide_reference_setup_transaction()

        # We shouldn't ACK immediately; we'll need to wait our interpacket delay.
        yield
        self.assertEqual((yield dut.ack), 0)

        # After our minimum interpacket delay, we should see an ACK.
        yield from self.advance_cycles(10)
        self.assertEqual((yield dut.ack), 1)



    @usb_domain_test_case
    def test_short_setup_packet(self):
        dut = self.dut

        # Before we receive anything, we shouldn't have a new packet.
        self.assertEqual((yield dut.new_packet), 0)

        # Provide our setup packet.
        yield from self.provide_packet(
            0b00101101, # PID: SETUP token.
            0b00000000, 0b00010000 # Address 0, endpoint 0, CRC
        )

        # Provide our data packet; but shorter than expected.
        yield from self.provide_packet(
            0b11000011,                                     # PID: DATA0
            0b00100011, 0b01000101, 0b01100111, 0b10001001, # DATA
            0b00011100, 0b00001110                          # CRC
        )

        # This shouldn't count as a valid setup packet.
        yield
        self.assertEqual((yield dut.new_packet), 0)


if __name__ == "__main__":
    unittest.main(warnings="ignore")