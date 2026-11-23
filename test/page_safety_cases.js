// Runs in the page_test VM with the production page functions and a simulated transport.
module.exports = async function pageSafetyCases() {
  const input = '[lightspeed]\n[neopixel case_leds]\npin: PA1\nchain_count: 60\n[lightspeed strip case]\noutput: neopixel case_leds';
  const imported = parseCfg(input);
  assert.equal(imported.errors.length, 0);
  P = sanitizeProject(imported.project);
  assert.equal(P.strips[0].count, 60);
  assert.doesNotMatch(cfgText(), /^leds:/m);
  assert.equal(parseCfg(cfgText()).project.strips[0].wholeChain, true);
  // Dispatch through the same input handlers used by the strip card.
  const card = { dataset: { i: '0' }, querySelector: () => null, querySelectorAll: () => [] };
  const field = { closest: () => card, dataset: { k: 'range' }, value: '2-12' };
  stripHandlers.input({target: field});
  assert.match(cfgText(), /^leds: 2-12$/m);
  field.value = '';
  stripHandlers.input({target: field});
  assert.equal(P.strips[0].count, 60);
  assert.doesNotMatch(cfgText(), /^leds:/m);

  let saved = cfgText(), commands = [], uploads = [];
  Sync.set(saved);
  P.brightness = P.brightness === .8 ? .6 : .8;
  Live.status = {auto_state: 'idle'};
  Live.objects = null;
  Klip.state = 'ready'; Klip.busy = false;
  Preview.on = true;
  Preview.req = async () => ({result: {state: 'ready'}});
  Preview.cmd = async () => false;
  uploadCfg = async (name, text) => uploads.push({name, text});
  await autoSave();
  assert.equal(Sync.text, saved);
  assert.equal(uploads.length, 0);
  assert.equal(AS.failed, true);
  Preview.cmd = async a => { commands.push(a); return true; };
  await autoSave();
  assert.equal(uploads.length, 1);
  assert.equal(Sync.text, cfgText());
  assert.equal(AS.failed, false);

  SIM.timer = {state: 'heating', dur: 0, until: 0};
  await sendStatePreview('heating', 0);
  Preview.overrides.case = 'solid';
  await setPreviewEnabled(false);
  assert.ok(commands.at(-1).some(a => a.action === 'state' && a.state === 'auto'));
  assert.ok(commands.at(-1).some(a => a.strip === 'case' && a.hold === MANUAL_HOLD));
  assert.equal(Preview.on, false);
  const before = commands.length;
  await stopSim();
  assert.equal(commands.length, before);
  Preview.on = true; Preview.scenesSent.add('custom');
  await setPreviewEnabled(false);
  assert.ok(commands.at(-1).some(a => a.action === 'scene_off' && a.name === 'custom'));

  // Manual controls are on the visible strip card even when there is no 3D canvas.
  P.strips[0].type = 'pin'; P.strips[0].chain = 'lamp';
  assert.match(stripCard(P.strips[0], 0), /data-pin-mode="solid"/);
  for (const mode of ['solid', 'off', '']) {
    await stripHandlers.click({target: {closest: selector => selector === '.strip' ? card : selector === '[data-pin-mode]' ? {dataset: {pinMode: mode}} : null}});
    assert.equal(Preview.overrides.case || '', mode);
    assert.equal(commands.at(-1).hold, mode ? MANUAL_HOLD : 0);
  }

  P = sanitizeProject(imported.project);
  const files = {'lightspeed.cfg': 'known-good'};
  let printing = false, statusReadable = true, infoState = 'ready', backupReadable = true;
  const posts = [];
  uploadCfg = async (name, text) => { files[name] = text; };
  fetchText = async path => {
    const name = path.split('/').pop();
    if (name.endsWith('.bak') && !backupReadable) throw Error('file unavailable');
    return files[name] ?? null;
  };
  Preview.req = async (path, opts) => {
    if (path.startsWith('/printer/objects/query?print_stats')) {
      if (!statusReadable) throw Error('status unavailable');
      return {result: {status: {print_stats: {state: printing ? 'printing' : 'standby'}, idle_timeout: {state: 'Idle'}}}};
    }
    if (path === '/printer/info') return {result: {state: infoState}};
    if (opts?.method === 'POST') {
      posts.push(path);
      if (path === '/printer/lightspeed/restart_if_idle') throw Error('restart response lost');
      throw Error('stop at simulated restart');
    }
    throw Error('unexpected request: ' + path);
  };
  confirmDlg = async () => { printing = true; return true; };
  await saveApply();
  assert.equal(files['lightspeed.cfg'], 'known-good');
  assert.equal(posts.length, 0);
  printing = false;
  confirmDlg = async () => true;
  await saveApply();
  assert.equal(files['lightspeed.cfg.bak'], 'known-good');
  assert.equal(SA.canRestore, true);
  assert.equal(posts.at(-1), '/printer/lightspeed/restart_if_idle');
  // New page session: only printer files and host-scoped localStorage survive.
  Object.assign(SA, {backup: null, keepBackup: false, canRestore: false, steps: [], note: ''});
  assert.equal(await loadRecoveryBackup(), true);
  assert.equal(SA.backup, 'known-good');
  await saveApply();
  assert.equal(files['lightspeed.cfg.bak'], 'known-good');
  const edited = files['lightspeed.cfg'];
  backupReadable = false;
  await saveApply();
  assert.equal(files['lightspeed.cfg'], edited);
  assert.equal(files['lightspeed.cfg.bak'], 'known-good');
  assert.equal(posts.length, 2);
  backupReadable = true;
  assert.equal(await loadRecoveryBackup(), true);
  printing = true;
  await restoreBackup();
  assert.notEqual(files['lightspeed.cfg'], 'known-good');
  assert.equal(SA.canRestore, true);
  printing = false; statusReadable = false;
  await assert.rejects(() => restartAndWait(() => {}), /idle/);
  assert.equal(posts.length, 2);
  infoState = 'error';
  await assert.rejects(() => restartAndWait(() => {}), /simulated restart/);
  assert.equal(posts.at(-1), '/printer/firmware_restart');

  // A fresh tab reopens the verified backup, restores the file, and clears
  // its recovery record only after a successful restart.
  Object.assign(SA, {backup: null, keepBackup: false, canRestore: false, steps: [], note: ''});
  assert.equal(await loadRecoveryBackup(), true);
  restartAndWait = async () => ({state: 'ready'});
  liveConnect = async () => {};
  await restoreBackup();
  assert.equal(files['lightspeed.cfg'], 'known-good');
  assert.equal(files['lightspeed.cfg.bak'], 'known-good');
  assert.equal(localStorage.getItem(recoveryKey()), null);
  assert.equal(SA.canRestore, false);

  Preview.req = async () => ({result: {state: 'merge_required', ok: false,
    candidate: 'lightspeed.migrated.cfg.txt'}});
  let restartPrompts = 0;
  klipRestart = async () => { restartPrompts++; };
  await leMigrate();
  assert.equal(LeMig.done, false);
  assert.match(LeMig.err, /lightspeed\.migrated\.cfg\.txt/);
  assert.equal(restartPrompts, 0);
};
