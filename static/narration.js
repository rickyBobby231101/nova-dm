/* Phase 11: play the DM's voice on this device.
 *
 * The server used to speak through its own sound card, so the browser had
 * nothing to do. Now it synthesizes a clip, announces it, and every device plays
 * it -- which puts two browser realities in the way.
 *
 * 1. Ordering. Clips are announced in synthesis order, but an <audio> element
 *    started the moment a clip lands would overlap the one still playing. So
 *    they queue here and play strictly one at a time, the same guarantee the
 *    server's worker queue provides on its side.
 *
 * 2. Autoplay. Phones refuse to play audio that no gesture asked for, and the
 *    refusal is silent -- the promise from play() just rejects. Since the whole
 *    point is a voiced DM on a phone, that has to be visible: the first blocked
 *    clip flips a flag the page turns into a "tap to enable" control, and the
 *    tap both unlocks audio and drains whatever queued up meanwhile.
 */
(function () {
  const MUTE_KEY = 'novadm.deviceMuted';

  const state = {
    queue: [],
    playing: false,
    blocked: false,               // autoplay refused; needs a gesture
    muted: localStorage.getItem(MUTE_KEY) === '1',
  };

  let audio = null;
  let listener = null;

  function notify() {
    if (listener) listener({ muted: state.muted, blocked: state.blocked, pending: state.queue.length });
  }

  function element() {
    if (!audio) {
      audio = new Audio();
      audio.preload = 'auto';
      // Whatever the outcome, move on: a clip that fails to decode must not
      // wedge every later line of narration behind it.
      audio.addEventListener('ended', next);
      audio.addEventListener('error', next);
    }
    return audio;
  }

  function next() {
    state.playing = false;
    drain();
  }

  function drain() {
    if (state.playing || state.muted || !state.queue.length) { notify(); return; }
    const clip = state.queue.shift();
    const el = element();
    el.src = clip.url;
    state.playing = true;
    const started = el.play();
    if (started && typeof started.catch === 'function') {
      started.catch((err) => {
        state.playing = false;
        if (err && err.name === 'NotAllowedError') {
          // Put it back -- it hasn't been heard, and the tap that unlocks audio
          // should replay from here rather than silently skipping a turn.
          state.queue.unshift(clip);
          state.blocked = true;
        }
        notify();
      });
    }
    notify();
  }

  const api = {
    /* A clip announced by the server. */
    enqueue(clip) {
      if (!clip || !clip.url) return;
      state.queue.push(clip);
      drain();
    },

    /* Call from a real user gesture (click/tap) to satisfy autoplay policy. */
    unlock() {
      state.blocked = false;
      const el = element();
      // Playing muted from a gesture is the reliable way to mark the element
      // as user-approved; the real clip then plays without a second gesture.
      el.muted = true;
      const probe = el.play();
      const finish = () => { el.pause(); el.muted = false; drain(); };
      if (probe && typeof probe.then === 'function') probe.then(finish).catch(finish);
      else finish();
    },

    setMuted(on) {
      state.muted = !!on;
      localStorage.setItem(MUTE_KEY, state.muted ? '1' : '0');
      if (state.muted) {
        state.queue.length = 0;
        if (audio) { audio.pause(); }
        state.playing = false;
      }
      notify();
      if (!state.muted) drain();
    },

    isMuted() { return state.muted; },
    isBlocked() { return state.blocked; },

    /* The page decides how to render mute/blocked state. */
    onState(fn) { listener = fn; notify(); },
  };

  window.NovaNarration = api;
})();
