"""
Translate AMI events to call events.

The ChannelManager takes AMI or AMI-like events and translates them to
more higher level events.

During operation, the ChannelManager instance is fed AMI events through::

    on_event(self, event)

If it determines that something interesting has happened, it fires one
of these two events::

    on_b_dial(self, caller, callee)
    on_transfer(self, transferor, party1, party1)

You should override the ChannelManager on_b_dial and on_transfer in your
subclass and add the desired behaviour for those events.
"""
from collections import defaultdict

from .callerid import CallerId


class MissingChannel(KeyError):
    pass


class MissingUniqueid(KeyError):
    pass


class BridgedError(Exception):
    pass


class Channel(object):
    """
    A Channel holds Asterisk channel state.

    It can be renamed, linked, tied to other channels, bridged,
    masqueraded and so on. All of the above is typical low level
    Asterisk channel behaviour.

    Together with the ChannelManager, the Channel keeps track of the
    state of all open channels through the events generated by a running
    Asterisk instance.
    """
    def __init__(self, event, channel_manager):
        """
        Create a new channel instance. Takes a channel manager managing
        it as argument for callbacks.

        Args:
            event (Dict): A dictionary with Asterisk AMI data. Only the
                Newchannel event should be passed. The ChannelManager
                does the other translations.
            channel_manager (ChannelManager): The channel manager that takes
                the AMI events and passes state changes to individual channels
                or groups of channels at once.

        Example::

            channel = Channel(
                event={
                    'AccountCode': '',
                    'CallerIDName': 'Foo bar',
                    'CallerIDNum': '+31501234567',
                    'Channel='SIP/voipgrid-siproute-dev-0000000c',
                    'ChannelState': '0',
                    'ChannelStateDesc': 'Down',
                    'Context': 'voipgrid_in',
                    'Event': 'Newchannel',
                    'Exten': '+31501234567',
                    'Privilege: 'call,all',
                    'Uniqueid': 'vgua0-dev-1442239323.24',
                    'content: '',
                },
                channel_manager=channel_manager)
        """
        self._channel_manager = channel_manager

        # Uses of this instance may put data in the custom dict. We take
        # care to link this on masquerade.
        self.custom = {}

        self._name = event['Channel']
        self._id = event['Uniqueid']
        self._next, self._prev = None, None

        self._state = int(event['ChannelState'])  # 0, Down
        self._bridged = set()
        self._accountcode = event['AccountCode']
        self._exten = event['Exten']

        # If this is a SIP/<accountcode>- channel, then this is an
        # outbound channel where the CLI is wrong. We could set the
        # accountcode, but we overwrite it in get_callerid later on
        # anyway.
        if (len(self._accountcode) == 9 and
                self._accountcode.isdigit() and
                event['Channel'].startswith(
                    'SIP/{}-'.format(event['AccountCode']))):
            # This is a destination channel. Set exten as CLI.
            self._callerid = CallerId(
                name='', number=self._exten)
        else:
            # This is a source channel? Or a non-SIP channel? Set as
            # much info as we have at this point.
            self._callerid = CallerId(
                code=int(self._accountcode or 0),
                name=event['CallerIDName'], number=event['CallerIDNum'],
                is_public=True)

        self._trace('new {!r}'.format(self))

    def __repr__(self):
        return (
            '<Channel('
            'name={self._name!r} '
            'id={self._id!r} '
            'next={next} prev={prev} '
            'state={self._state} '
            'accountcode={self._accountcode} '
            'cli={self._callerid} '
            'exten={self._exten!r})>').format(
                self=self,
                next=(self._next and self._next.name),
                prev=(self._prev and self._prev.name))

    def _trace(self, msg):
        """
        _trace can be used to follow interesting events.
        """
        pass

    @property
    def is_relevant(self):
        """
        is_relevant returns true if the channel is not a Zombie channel.

        Returns:
            bool: True if self is a SIP channel and not a zombie channel.
        """
        return (self.name.startswith('SIP/') and
                not self.name.endswith('<ZOMBIE>'))

    @property
    def uniqueid(self):
        return self._id

    @property
    def name(self):
        return self._name

    @property
    def callerid(self):
        # Unconditionally(!) replace accountcode if the channel has it.
        if self._name.startswith('SIP/'):
            if (self._name[13:14] == '-' and  # SIP/<accountcode>-
                    self._name[4:13].isdigit()):
                return self._callerid.replace(code=int(self._name[4:13]))
            else:
                return self._callerid.replace(code=0)
        return self._callerid

    @property
    def accountcode(self):
        return self._accountcode

    @property
    def is_bridged(self):
        return bool(self._bridged)

    @property
    def bridged_channel(self):
        tmp = list(self._bridged)
        if len(tmp) != 1:
            raise BridgedError(
                'Expected one bridged channel. '
                'Did Asterisk bridge multiple? '
                'Or did you forget to call is_bridged? '
                'Bridge set: {!r}'.format(self._bridged))
        return tmp[0]

    def set_name(self, name):
        """
        set_name changes _name of ``self`` to ``name``.

        Args:
            name (str): The name that this channel should have.
        """
        old_name = self._name
        self._name = name
        self._trace('set_name {} -> {}'.format(old_name, name))

    def set_state(self, event):
        """
        set_state changes _state of ``self`` to the ChannelState in ``event``.
        If the channel state changes, it calls meth:`_raw_a_dial`
        and/or meth:`_raw_b_dial` if needed.

        Args:
            event (dict): A dictionary containing an AMI event.

        Example event:

            <Message CallerIDName='Foo bar' CallerIDNum='+31501234567'
            Channel='SIP/voipgrid-siproute-dev-0000000c' ChannelState='4'
            ChannelStateDesc='Ring' ConnectedLineName=''
            ConnectedLineNum='' Event='Newstate' Privilege='call,all'
            Uniqueid='vgua0-dev-1442239323.24' content=''>

        Asterisk ChannelStates:

            AST_STATE_DOWN = 0,
            AST_STATE_RESERVED = 1,
            AST_STATE_OFFHOOK = 2,
            AST_STATE_DIALING = 3,
            AST_STATE_RING = 4,
            AST_STATE_RINGING 5,
            AST_STATE_UP = 6,
            AST_STATE_BUSY = 7,
            AST_STATE_DIALING_OFFHOOK = 8,
            AST_STATE_PRERING = 9
        """
        old_state = self._state
        self._state = int(event['ChannelState'])  # 4=Ring, 6=Up
        assert old_state != self._state
        self._trace('set_state {} -> {}'.format(old_state, self._state))

        if old_state == 0:
            if self._state in (3, 4, 6):
                self._channel_manager._raw_a_dial(self)
            if self._state in (5, 6):
                self._channel_manager._raw_b_dial(self)

    def set_callerid(self, event):
        """
        set_callerid sets a class:`CallerId` object as attr:`_callerid`
        according to the relevant variables in `event`.

        Args:
            event (dict): A dictionary containing an AMI event.

        Example event:

            <Message CID-CallingPres='1 (Presentation Allowed, Passed
            Screen)' CallerIDName='Foo bar' CallerIDNum='+31501234567'
            Channel='SIP/voipgrid-siproute-dev-0000000c'
            Event='NewCallerid' Privilege='call,all'
            Uniqueid='vgua0-dev-1442239323.24' content=''>
        """
        old_cli = str(self._callerid)
        self._callerid = CallerId(
            code=self._callerid.code,
            name=event['CallerIDName'], number=event['CallerIDNum'],
            is_public=('Allowed' in event['CID-CallingPres']))
        self._trace('set_callerid {} -> {}'.format(old_cli, self._callerid))

    def set_accountcode(self, event):
        """
        set_accountcode sets attr:`_accountcode` to the 'Accountcode' defined
        in `event`.

        Args:
            event (dict): A dictionary containing an AMI event.

        Example event:

            <Message AccountCode='12668'
            Channel='SIP/voipgrid-siproute-dev-0000000c'
            Event='NewAccountCode' Privilege='call,all'
            Uniqueid='vgua0-dev-1442239323.24' content=''>
        """
        old_code = self._accountcode
        self._accountcode = event['AccountCode']
        self._trace('set_accountcode {} -> {}'.format(
            old_code, self._accountcode))

    def do_hangup(self, event):
        """
        do_hangup clears clears all related channel and raises an error if any
        channels were bridged.

        Args:
            event (dict): A dictionary containing an AMI event.
        """
        if self._next:
            self._next._prev = None
        if self._prev:
            self._prev._next = None
        # Assert that there are no bridged channels.
        assert not self._bridged, self._bridged

    def do_localbridge(self, other):
        """
        do_localbridge sets `self` as attr:`_prev` on `other` and other as
        attr:`_next` on `self`.

        Args:
            other (Channel): An instance of class:`Channel`.

        Example event:

            <Message
            Channel1='Local/ID2@osvpi_route_phoneaccount-00000006;1'
            Channel2='Local/ID2@osvpi_route_phoneaccount-00000006;2'
            Context='osvpi_route_phoneaccount' Event='LocalBridge'
            Exten='ID2' LocalOptimization='Yes' Privilege='call,all'
            Uniqueid1='vgua0-dev-1442239323.25'
            Uniqueid2='vgua0-dev-1442239323.26' content=''>
        """
        assert self._next is None, self._next
        assert self._prev is None, self._prev
        self._next = other
        assert other._next is None, other._next
        assert other._prev is None, other._prev
        other._prev = self
        self._trace('do_localbridge -> {!r}'.format(other))

    def do_masquerade(self, other):
        """
        do_masquerade removes all links from `self` and moves the links from
        `other` to `self`. The `custom` dict is also moved from `other` to
        `self`.

        Args:
            other (Channel): An instance of class:`Channel`.
        """
        # If self is linked, we must undo all of that first.
        if self._next:
            self._trace('discarding old next link {}'.format(self._next.name))
            self._next._prev = None
            self._next = None
        if self._prev:
            self._trace('discarding old prev link {}'.format(self._prev.name))
            self._prev._next = None
            self._prev = None

        # If other is linked, move that to us.
        if other._next:
            other._next._prev = self
            self._next = other._next
            other._next = None
            self._trace('updated next link {}'.format(self._next.name))
        if other._prev:
            other._prev._next = self
            self._prev = other._prev
            other._prev = None
            self._trace('updated prev link {}'.format(self._prev.name))

        # What should we do with bridges? In the Asterisk source, it looks like
        # we keep the bridges intact, i.e.: the original (self) channel gets
        # properties copied from the clone (other), while we leave the bridging
        # in tact. That would mean that any bridges on the clone would be
        # destroyed later on.

        # There is one interesting feature going on here, later on, in
        # certain cases, we a get a soon to be destroyed channel that we
        # need to write info to. We link the info dict to the new class
        # so we can write to the old one.
        self.custom = other.custom

        self._trace('do_masquerade -> {!r}'.format(self))

    def do_link(self, other):
        """
        do_link adds `other` to the set of bridged channels in `self` and vice
        versa.

        Args:
            other (Channel): An instance of class:`Channel`.
        """
        self._bridged.add(other)
        other._bridged.add(self)

    def do_unlink(self, other):
        """
        do_link removes `other` from the set of bridged channels in `self` and
        vice versa.

        Args:
            other (Channel): An instance of class:`Channel`.
        """
        self._bridged.remove(other)
        other._bridged.remove(self)

    def get_dialing_channel(self):
        """
        Figure out on whose channel's behalf we're calling.

        When a channel is not bridged yet, you can use this on the
        B-channel to figure out which A-channel initiated the call.

        * For every dial, a "perpetrator" entry is added in
         _dial_bcklink.
        * We look for those, while backwards over locally linked
          channels (the _prev entries).
        """
        a_chan = self
        revdials = self._channel_manager._dial_bcklink

        # We can do without recursion this time, since there will be
        # only one result.
        while True:
            try:
                dialing_uniqueid = revdials[a_chan.uniqueid]
            except KeyError:
                break
            else:
                a_chan = self._channel_manager._get_chan_by_uniqueid(dialing_uniqueid)
                # Likely, the a_chan._prev is None, in which case we're
                # looking at the source channel. Or, the a_chan as one
                # _next, after which we will find a result in
                # self._dial_bcklink.
                if not a_chan._prev:
                    assert a_chan.uniqueid not in revdials
                    break
                while a_chan._prev:
                    a_chan = a_chan._prev
                    assert not a_chan._prev, \
                        ('Since when does asterisk do double links? '
                         'a_chan={!r}'.format(a_chan))

        return a_chan

    def get_dialed_channels(self):
        """
        Figure out which channels are calling on our behalf.

        When a channel is not bridged yet, you can use this on the
        A-channel to find out which channels are dialed on behalf of
        this channel.

        It works like this:

        * A-channel (this) has a list of dial_fwdlink items (open
          dials).
        * We loop over those (a list of uniqueids) and find the
          corresponding channels.
        * Those channels may be SIP channels, or they can be local
          channels, in which case we have to look further (by calling
          this function on those channels).
        """
        b_channels = set()
        dials = self._channel_manager._dial_fwdlink

        for dialed_uniqueid in dials[self.uniqueid]:
            b_chan = self._channel_manager._get_chan_by_uniqueid(dialed_uniqueid)
            # Likely, the b_chan._next is None, in which case we're
            # looking at a real tech channel (non-Local). Or, the
            # b_chan has one _next, after which we have to call this
            # function again.
            if not b_chan._next:
                assert b_chan.uniqueid not in dials
                b_channels.add(b_chan)
            else:
                while b_chan._next:
                    b_chan = b_chan._next
                    assert not b_chan._next, \
                        ('Since when does asterisk do double links? '
                         'b_chan={!r}'.format(b_chan))
                assert not b_chan._next
                b_channels.update(b_chan.get_dialed_channels())

        return b_channels

    def get_related(self, used=None):
        """
        Get all channels related to this channel. Uses a depth first
        search which is sufficient for our purposes.

        NOTE: This function is only used in assertion checks. We can
        probably drop this when finalizing the channel manager.
        """
        if not used:
            used = set()

        if self not in used:
            used.add(self)

            if self._prev:
                self._prev.get_related(used)
            if self._next:
                self._next.get_related(used)
            for bridged in self._bridged:
                bridged.get_related(used)

        return used

    def get_relevant(self):
        """
        NOTE: This function is only used in assertion checks. We can
        probably drop this when finalizing the channel manager.
        """
        related = set(self.get_related())
        return tuple(sorted(
            [i for i in related if i.is_relevant],
            key=(lambda x: x.name)))


class ChannelManager(object):
    """
    The ChannelManager translates AMI events to high level call events.

    Usage::

        class MyChannelManager(ChannelManager):
            def on_b_dial(self, caller, callee):
                # Your code here. All arguments (except self) are of
                # type CallerId.
                pass

            def on_transfer(transferor, party1, party1):
                # Your code here. All arguments (except self) are of
                # type CallerId.
                pass

        class MyReporter(object):
            def trace_ami(self, ami):
                print(ami)

            def trace_msg(self, msg):
                print(msg)

        manager = MyChannelManager(MyReporter())

        # events is a list of AMI-event-like dictionaries.
        for event in events:
            if ('*' in manager.INTERESTING_EVENTS or
                    event['Event'] in manager.INTERESTING_EVENTS):
                # After some of the events, an on_b_dial() or
                # on_transfer() will be called.
                manager.on_event(event)
    """
    # We require all of these events to function properly. (Except
    # perhaps the FullyBooted one.)
    INTERESTING_EVENTS = (
        # This tells us that we're connected. We should probably
        # flush our channels at this point, because they aren't up
        # to date.
        'FullyBooted',
        # These events all relate to low level channel setup and
        # maintenance.
        'Newchannel', 'Newstate', 'NewCallerid',
        'NewAccountCode', 'LocalBridge', 'Rename',
        'Bridge', 'Masquerade',
        # Higher level channel info.
        'Dial', 'Hangup', 'Transfer',
        # UserEvents
        'UserEvent'
    )

    def __init__(self, reporter):
        """
        Create a ChannelManager instance.

        Args:
            reporter (Reporter): A reporter with trace_msg and trace_ami
                methods.
        """
        self._reporter = reporter
        self._channels = {}
        self._channels_by_uniqueid = {}
        self._dial_fwdlink = defaultdict(list)  # A-chan: [B-chan]
        self._dial_bcklink = {}                 # B-chan: A-chan

    def _get_chan_by_channame_from_evkey(self, event, event_key, pop=False):
        """
        _get_chan_by_channame_from_evkey returns the channel at `event_key` in
        `event`. If the Channel can not be found a MissingChannel error is
        raised.

        Args:
            event (dict): A dictionary containing an AMI event.
            event_key (str): The key to look up in event.
            pop (bool): Pop the item from event if True.
        """
        value = event[event_key]
        try:
            if pop:
                return self._channels.pop(value)
            return self._channels[value]
        except KeyError:
            raise MissingChannel(event_key, value)

    def _get_chan_by_uniqueid(self, uniqueid):
        try:
            return self._channels_by_uniqueid[uniqueid]
        except KeyError:
            raise MissingUniqueid(uniqueid)

    def on_event(self, event):
        """
        on_event calls `_on_event` with `event`. If `_on_event` raisen an
        exception this is logged.

        Args:
            event (dict): A dictionary containing an AMI event.
        """
        try:
            self._on_event(event)
        except MissingChannel as e:
            # If this is after a recent FullyBooted and/or start of
            # self, it is reasonable to expect that certain events will
            # fail.
            self._reporter.trace_msg(
                'Channel {}={!r} not in mem when processing event: '
                '{!r}'.format(e.args[0], e.args[1], event))
        except MissingUniqueid as e:
            # This too is reasonably expected.
            self._reporter.trace_msg(
                'Channel with Uniqueid {} not in mem when processing event: '
                '{!r}'.format(e.args[0], event))
        except BridgedError as e:
            self._reporter.trace_msg(e)

        self._reporter.on_event(event)

    def _on_event(self, event):
        """
        on_event takes an event, extract and store the appropriate state
        updates and if possible fire an event ourself.

        Args:
            event (Dict): A dictionary with Asterisk AMI data.
        """
        # Write message to reporter, for debug/test purposes.
        self._reporter.trace_ami(event)

        event_name = event['Event']

        if event_name == 'FullyBooted':
            # Time to clear our channels because they are stale?
            self._reporter.trace_msg('Connected to Asterisk')
        elif event_name == 'Newchannel':
            channel = Channel(event, channel_manager=self)
            self._channels[channel.name] = channel
            self._channels_by_uniqueid[channel.uniqueid] = channel
        elif event_name == 'Newstate':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel')
            channel.set_state(event)
        elif event_name == 'NewCallerid':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel')
            channel.set_callerid(event)
        elif event_name == 'NewAccountCode':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel')
            channel.set_accountcode(event)
        elif event_name == 'LocalBridge':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel1')
            other = self._get_chan_by_channame_from_evkey(event, 'Channel2')
            channel.do_localbridge(other)
        elif event_name == 'Rename':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel', pop=True)
            channel.set_name(event['Newname'])
            self._channels[channel.name] = channel
        elif event_name in 'Bridge':
            channel1 = self._get_chan_by_channame_from_evkey(event, 'Channel1')
            channel2 = self._get_chan_by_channame_from_evkey(event, 'Channel2')
            if event['Bridgestate'] == 'Link':
                channel1.do_link(channel2)
            elif event['Bridgestate'] == 'Unlink':
                channel1.do_unlink(channel2)
            else:
                assert False, event
        elif event_name == 'Masquerade':
            # A Masquerade destroys the Original and puts the guts of
            # Clone into it. Afterwards, the Clone channel will be
            # removed.
            clone = self._get_chan_by_channame_from_evkey(event, 'Clone')
            original = self._get_chan_by_channame_from_evkey(event, 'Original')

            if event['CloneState'] != event['OriginalState']:
                # For blonde transfers, the original state is Ring.
                assert event['OriginalState'] in ('Ring', 'Ringing')
                assert event['CloneState'] == 'Up', event

                # This is a call pickup?
                if event['OriginalState'] == 'Ringing':
                    self._raw_pickup_transfer(winner=clone, loser=original)

            original.do_masquerade(clone)
        elif event_name == 'Hangup':
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel')
            before = channel.get_relevant()  # TEMP for assertion only

            channel.do_hangup(event)
            del self._channels[channel.name]
            del self._channels_by_uniqueid[channel.uniqueid]

            # Extra sanity checks.
            for chan in self._channels.values():
                assert chan._next != channel, chan
                assert chan._prev != channel, chan
            after = channel.get_relevant()  # TEMP for assertion only
            assert before == after, (before, after)

            if not self._channels:
                assert not self._channels_by_uniqueid
                assert not self._dial_bcklink
                assert not self._dial_fwdlink
                self._reporter.trace_msg('(no channels left)')

            if channel.uniqueid in self._dial_bcklink:
                a_chan = self._dial_bcklink.pop(channel.uniqueid)
                self._dial_fwdlink[a_chan].remove(channel.uniqueid)
                if not self._dial_fwdlink[a_chan]:
                    del self._dial_fwdlink[a_chan]

        elif event_name == 'Dial':
            if event['SubEvent'] == 'Begin':
                self._get_chan_by_uniqueid(event['UniqueID'])
                self._get_chan_by_uniqueid(event['DestUniqueID'])
                assert event['DestUniqueID'] not in self._dial_bcklink
                # fwdlink contains a list of all B-channels that
                # A-channels has dialed.
                self._dial_fwdlink[event['UniqueID']].append(event['DestUniqueID'])
                # bcklink contains a mapping of B-channels to
                # A-channels that dialed them.
                self._dial_bcklink[event['DestUniqueID']] = event['UniqueID']
            elif event['SubEvent'] == 'End':
                # This is cleaned up at Hangup.
                pass
            else:
                assert False, event

        elif event_name == 'Transfer':
            # Both TargetChannel and TargetUniqueid can be used to match
            # the target channel; they can be used interchangeably.
            channel = self._get_chan_by_channame_from_evkey(event, 'Channel')
            target = self._get_chan_by_channame_from_evkey(event, 'TargetChannel')
            assert target == self._channels_by_uniqueid[event['TargetUniqueid']]
            if event['TransferType'] == 'Attended':
                self._raw_attended_transfer(channel, target)
            elif event['TransferType'] == 'Blind':
                self._raw_blind_transfer(channel, target, event['TransferExten'])
            else:
                raise NotImplementedError(event)
        else:
            pass

    # ===================================================================
    # Event handler translators
    # ===================================================================

    def _raw_a_dial(self, channel):
        # We don't want this. It's work to get all the values right, and
        # when we do, this would look just like on_b_dial.
        # Further, the work we do to get on_transfer right makes getting
        # consistent on_a_dials right even harder.
        pass

    def _raw_b_dial(self, channel):
        if channel.name.startswith('SIP/'):
            a_chan = channel.get_dialing_channel()
            b_chan = channel
            callee = b_chan.callerid

            if 'raw_blind_transfer' in a_chan.custom:
                # This is an interesting exception: we got a Blind
                # Transfer message earlier and recorded it in this
                # attribute. We'll translate this b_dial to first a
                # on_b_dial and then the on_transfer event.
                old_a_chan = a_chan.custom.pop('raw_blind_transfer')

                caller = old_a_chan.callerid
                self.on_b_dial(caller, callee)

                redirector = caller
                caller = a_chan.callerid
                self.on_transfer(redirector, caller, callee)
            else:
                caller = a_chan.callerid
                self.on_b_dial(caller, callee)

    def _raw_attended_transfer(self, channel, target):
        redirector = target.callerid
        a_chan = channel.bridged_channel
        caller = a_chan.callerid

        if target.is_bridged:
            # The channel is bridged, things are easy.
            # (Attended transfer.)
            b_chan = target.bridged_channel
            b_chan._fired_on_b_dial = caller
            callee = b_chan.callerid

            self.on_transfer(redirector, caller, callee)
        else:
            # The second channel is not bridged. Check the open dials.
            # (Blonde transfer.)
            for b_chan in target.get_dialed_channels():
                callee = b_chan.callerid
                self.on_transfer(redirector, caller, callee)

    def _raw_blind_transfer(self, channel, target, targetexten):
        # This Transfer event is earlier than the dial. We mark it and
        # wait for the b_dial event. In on_b_dial we send out both the
        # on_b_dial and the on_transfer.
        target.custom['raw_blind_transfer'] = channel

    def _raw_pickup_transfer(self, winner, loser):
        a_chan = loser.get_dialing_channel()
        caller = a_chan.callerid

        # The CLI of winner cannot be set properly. It has dialed in, so
        # we have no CLI. Whatever is in there is wrong. Instead, we
        # provide the destination details of loser, since that is what's
        # used to dial in.
        dest = loser.callerid
        callee = winner.callerid.replace(
            name=dest.name, number=dest.number, is_public=dest.is_public)

        # Call on_transfer, with callee twice, since the callee is the
        # cause of the transfer (loser had nothing to do with it).
        self.on_transfer(callee, caller, callee)

    # ===================================================================
    # Actual event handlers you should override
    # ===================================================================

    def on_b_dial(self, caller, callee):
        """
        Gets invoked when the B side of a call is initiated.

        In the common case, calls in Asterisk consist of two sides: A
        calls Asterisk and Asterisk calls B. This event is fired when
        Asterisk performs the second step.

        Args:
            caller (CallerId): The initiator of the call.
            callee (CallerId): The recipient of the call.
        """
        self._reporter.on_b_dial(caller, callee)

        self._reporter.trace_msg(
            'b_dial: {} --> {}'.format(caller, callee))

    def on_transfer(self, redirector, party1, party2):
        """
        Gets invoked when a call is transferred.

        In the common case, a call transfer consists of three parties
        where the redirector was speaking to party1 and party2. By
        transferring the call, he ties party1 and party2 together and
        leaves himself.

        But there are other cases, including the case where the
        redirector is the party that takes an incoming call and places
        himself on end of the bridge. In that case he is both the
        redirector and one of party1 or party2.

        Args:
            redirector (CallerId): The initiator of the transfer.
            party1 (CallerId): One of the two parties that are tied
                together.
            party2 (CallerId): The other one.
        """
        self._reporter.trace_msg(
            'transfer: {} <--> {} (through {})'.format(
                party1, party2, redirector))


class DebugChannelManager(ChannelManager):
    """
    DebugChannel functions exactly like the default class:`ChannelManager`. The
    only difference is that this ChannelManager acts on all events, instead of
    dropping all events that are deemed 'not interesting'. This is usefull for
    creating debug logs.
    """
    INTERESTING_EVENTS = ('*',)
