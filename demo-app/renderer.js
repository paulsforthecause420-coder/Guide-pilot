/* Deterministic demo behaviour: validation, save, dialog, reset. */

const form = document.getElementById('config-form');
const appName = document.getElementById('app-name');
const callbackUrl = document.getElementById('callback-url');
const region = document.getElementById('region');
const telemetry = document.getElementById('telemetry');
const strictMode = document.getElementById('strict-mode');
const saveButton = document.getElementById('save-button');
const advancedButton = document.getElementById('advanced-button');
const resetButton = document.getElementById('reset-button');
const alertBox = document.getElementById('alert-box');
const statusBox = document.getElementById('status-box');
const loading = document.getElementById('loading');
const summarySection = document.getElementById('summary-section');
const summaryList = document.getElementById('summary-list');
const backdrop = document.getElementById('dialog-backdrop');
const dialogStrict = document.getElementById('dialog-strict');
const dialogApply = document.getElementById('dialog-apply');
const dialogCancel = document.getElementById('dialog-cancel');

let saved = false;
let saveTimer = null;

function report(phase, extra = {}) {
  const state = {
    phase,
    saved,
    appName: appName.value,
    callbackUrl: callbackUrl.value,
    region: region.value,
    telemetry: telemetry.checked,
    strictMode: strictMode.checked,
    dialogOpen: !backdrop.hidden,
    errorText: alertBox.hidden ? '' : alertBox.textContent,
    statusText: statusBox.hidden ? '' : statusBox.textContent,
    ...extra,
  };
  if (window.demo) window.demo.reportState(state);
}

function showAlert(message) {
  alertBox.textContent = message;
  alertBox.hidden = false;
  statusBox.hidden = true;
}

function clearAlert() {
  alertBox.textContent = '';
  alertBox.hidden = true;
}

function showStatus(message) {
  statusBox.textContent = message;
  statusBox.hidden = false;
}

function resetForm() {
  clearAlert();
  statusBox.hidden = true;
  statusBox.textContent = '';
  summarySection.hidden = true;
  summaryList.innerHTML = '';
  form.reset();
  saved = false;
  saveButton.disabled = false;
  loading.hidden = true;
  report('reset');
}

function renderSummary() {
  const entries = [
    ['App Name', appName.value],
    ['Callback URL', callbackUrl.value],
    ['Region', region.value || '(none)'],
    ['Telemetry', telemetry.checked ? 'Enabled' : 'Disabled'],
    ['Strict mode', strictMode.checked ? 'Enabled' : 'Disabled'],
  ];
  summaryList.innerHTML = '';
  for (const [label, value] of entries) {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    summaryList.append(dt, dd);
  }
  summarySection.hidden = false;
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  clearAlert();
  if (!appName.value.trim()) {
    showAlert('App Name is required.');
    appName.focus();
    report('validation-error');
    return;
  }
  if (!/^https:\/\/.+/.test(callbackUrl.value.trim())) {
    showAlert('Callback URL must start with https://');
    callbackUrl.focus();
    report('validation-error');
    return;
  }
  if (!region.value) {
    showAlert('Choose a region before saving.');
    region.focus();
    report('validation-error');
    return;
  }
  saveButton.disabled = true;
  loading.hidden = false;
  report('saving');
  saveTimer = setTimeout(() => {
    loading.hidden = true;
    saveButton.disabled = false;
    saved = true;
    showStatus('Configuration saved successfully');
    renderSummary();
    report('saved');
  }, 350);
});

advancedButton.addEventListener('click', () => {
  dialogStrict.checked = strictMode.checked;
  backdrop.hidden = false;
  dialogStrict.focus();
  report('dialog-open');
});

dialogApply.addEventListener('click', () => {
  strictMode.checked = dialogStrict.checked;
  backdrop.hidden = true;
  report('dialog-applied');
});

dialogCancel.addEventListener('click', () => {
  backdrop.hidden = true;
  report('dialog-cancelled');
});

resetButton.addEventListener('click', () => {
  if (saveTimer) clearTimeout(saveTimer);
  resetForm();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !backdrop.hidden) {
    backdrop.hidden = true;
    report('dialog-cancelled');
  }
});

// Report on edits so automated tests can assert that input really arrived.
for (const input of [appName, callbackUrl, region, telemetry, strictMode]) {
  input.addEventListener('input', () => report('input'));
  input.addEventListener('change', () => report('change'));
}

report('ready');
