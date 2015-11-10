codplayer-control releases
==========================

1.1 2015-03-08
--------------

### New features

Discs can be linked as an alias for another disc.  An example usecase
is to play a remastered CD when inserting the original release.

If a linked disc is played, the source disc ID is available in the
`state.State.source_disc_id` parameter, and it can be played by
clicking on the SRC button.


### Other fixes

* https://github.com/petli/codplayer/issues/33: highlighting broken
  when discs have skipped tracks.


1.0 2014-09-16
--------------

* Timeout of 30 seconds after the last client disconnects before
  stopping the state subscriptions


0.9 2014-07-13
--------------

First fully packaged release.
