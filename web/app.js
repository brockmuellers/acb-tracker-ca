'use strict';

const PYODIDE_CDN = 'https://cdn.jsdelivr.net/pyodide/v0.29.4/full/pyodide.js';

// Broker display names; 'normalized' is handled inline without a fetched mapping file.
const BROKERS = {
  normalized: 'Normalized ACB CSV',
  schwab: 'Schwab',
  vanguard: 'Vanguard',
  wealthsimple: 'Wealthsimple',
  manual: 'Manual',
};

// Passthrough mapping for CSVs already in the ACB normalized format.
const NORMALIZED_MAPPING = `
column_map:
  date: date
  ticker: ticker
  type: type
  quantity: quantity
  price: price
  currency: currency
  exchange_rate: exchange_rate
  account_number: account_number
  time: time
  superficial_qty: superficial_qty
optional_columns:
  - currency
  - exchange_rate
  - account_number
  - time
  - superficial_qty
`.trim();

const OUTPUT_COLS = [
  'account_number', 'date', 'ticker', 'type', 'quantity', 'price',
  'currency', 'exchange_rate', 'amount_cad', 'acb_cad', 'gain_loss_cad', 'superficial_loss_cad',
];
const HOLDINGS_COLS = ['account_number', 'ticker', 'quantity', 'acb_cad'];

let pyRunPipeline = null;
const mappingCache = {};

// ---------------------------------------------------------------------------
// Sample data
// ---------------------------------------------------------------------------

const SAMPLE_MANUAL_CSV = [
  'Account Number,Settlement Date,Transaction Type,Symbol,Shares,Share Price,Amount,Currency,Notes,Time',
  'DEMO-1,2024-01-02,Start,VTI,50,210.00,10500.00,USD,,',
  'DEMO-1,2024-01-02,Start,,250.00,1.00,250.00,USD,,',
  'DEMO-1,2024-04-10,Buy,VTI,10,225.00,2250.00,USD,,',
  'DEMO-1,2024-07-15,Dividend,VTI,,,95.00,USD,,',
  'DEMO-1,2024-10-08,Sell,VTI,15,240.00,3600.00,USD,,',
].join('\n');

const SAMPLE_SCHWAB_CSV = [
  'Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount,Notes,Time',
  '01/10/2024,Buy,VXUS,Vanguard Total International Stock ETF,50,55.00,,-2750.00,,',
  '03/25/2024,Reinvest Dividend,VXUS,Vanguard Total International Stock ETF,,,,44.12,,',
  '03/25/2024,Reinvest Shares,VXUS,Vanguard Total International Stock ETF,0.789,55.92,,-44.12,,',
  '10/01/2024,Cash Dividend,VXUS,Vanguard Total International Stock ETF,,,,38.20,,',
  '12/05/2024,Sell,VXUS,Vanguard Total International Stock ETF,10,62.00,,620.00,,',
].join('\n');

const SAMPLE_SOURCES = [
  { broker: 'manual', csv: SAMPLE_MANUAL_CSV, account: 'DEMO-1' },
  { broker: 'schwab', csv: SAMPLE_SCHWAB_CSV, account: 'DEMO-2' },
];

function baseUrl() {
  return (window.ACB_BASE_URL || '..').replace(/\/$/, '');
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

async function init() {
  const statusEl = document.getElementById('acb-status');
  const formEl = document.getElementById('acb-form');

  const setStatus = msg => { statusEl.textContent = msg; };

  try {
    setStatus('Loading Python runtime (this takes a few seconds)…');

    await loadScript(PYODIDE_CDN);
    const pyodide = await window.loadPyodide();

    setStatus('Loading packages…');
    await pyodide.loadPackage('pyyaml');

    setStatus('Loading calculator…');
    const base = baseUrl();
    const pyFiles = [
      ['acb_lib.py',      `${base}/acb_lib.py`],
      ['translate_lib.py', `${base}/translate_lib.py`],
      ['acb_web.py',      `${base}/web/acb_web.py`],
    ];
    for (const [name, url] of pyFiles) {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`Failed to fetch ${name} (${resp.status})`);
      pyodide.FS.writeFile(name, await resp.text());
    }

    pyodide.runPython('from acb_web import run_pipeline');
    pyRunPipeline = pyodide.globals.get('run_pipeline');

    statusEl.style.display = 'none';
    formEl.style.display = 'block';

  } catch (err) {
    statusEl.textContent = `Initialization failed: ${err.message}`;
    statusEl.classList.add('acb-error');
  }
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(s);
  });
}

// ---------------------------------------------------------------------------
// Source rows
// ---------------------------------------------------------------------------

function buildSourceRow() {
  const div = document.createElement('div');
  div.className = 'acb-source';

  const brokerOptions = Object.entries(BROKERS)
    .map(([v, l]) => `<option value="${v}">${l}</option>`)
    .join('');

  div.innerHTML = `
    <div class="acb-source-fields">
      <label>Broker
        <select class="acb-broker">${brokerOptions}</select>
      </label>
      <label>CSV file(s)
        <input type="file" class="acb-files" accept=".csv" multiple required>
        <span class="acb-sample-badge" style="display:none">sample data loaded</span>
      </label>
      <label>Account number <span class="acb-hint">(optional)</span>
        <input type="text" class="acb-account" placeholder="e.g. 12345">
      </label>
      <button type="button" class="acb-remove-source">Remove</button>
    </div>
    <div class="acb-mapping-row">
      <label>Mapping YAML <span class="acb-hint">(auto-loaded from broker selection; editable)</span>
        <textarea class="acb-mapping" rows="10" spellcheck="false"></textarea>
      </label>
    </div>
  `;

  div.querySelector('.acb-broker').addEventListener('change', () => populateMappingTextarea(div));
  div.querySelector('.acb-files').addEventListener('change', () => {
    div._sampleCsv = null;
    div.querySelector('.acb-sample-badge').style.display = 'none';
  });
  return div;
}

async function populateMappingTextarea(sourceEl) {
  const broker = sourceEl.querySelector('.acb-broker').value;
  const textarea = sourceEl.querySelector('.acb-mapping');
  try {
    textarea.value = await fetchMapping(broker);
  } catch (e) {
    textarea.value = `# Error loading mapping: ${e.message}`;
  }
}

async function addSource() {
  const row = buildSourceRow();
  document.getElementById('acb-sources').appendChild(row);
  updateRemoveButtons();
  await populateMappingTextarea(row);
}

function updateRemoveButtons() {
  const sources = document.querySelectorAll('.acb-source');
  sources.forEach(s => {
    s.querySelector('.acb-remove-source').style.display = sources.length > 1 ? '' : 'none';
  });
}

async function loadSampleData() {
  const container = document.getElementById('acb-sources');
  container.innerHTML = '';

  for (const sample of SAMPLE_SOURCES) {
    const row = buildSourceRow();
    container.appendChild(row);
    row.querySelector('.acb-broker').value = sample.broker;
    row.querySelector('.acb-account').value = sample.account;
    await populateMappingTextarea(row);
    row._sampleCsv = sample.csv;
    row.querySelector('.acb-sample-badge').style.display = '';
  }

  updateRemoveButtons();
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

async function fetchMapping(broker) {
  if (broker === 'normalized') return NORMALIZED_MAPPING;
  if (!mappingCache[broker]) {
    const url = `${baseUrl()}/mappings/${broker}.yaml`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Failed to fetch mapping for "${broker}" (${resp.status})`);
    mappingCache[broker] = await resp.text();
  }
  return mappingCache[broker];
}

async function handleRun() {
  const runBtn = document.getElementById('acb-run');
  const errorEl = document.getElementById('acb-error');
  const warningsEl = document.getElementById('acb-warnings');
  const resultsEl = document.getElementById('acb-results');

  errorEl.style.display = 'none';
  warningsEl.style.display = 'none';
  resultsEl.style.display = 'none';
  runBtn.disabled = true;
  runBtn.textContent = 'Running…';

  try {
    const sources = [];
    for (const sourceEl of document.querySelectorAll('.acb-source')) {
      const broker = sourceEl.querySelector('.acb-broker').value;
      const files = sourceEl.querySelector('.acb-files').files;
      const accountNumber = sourceEl.querySelector('.acb-account').value.trim();
      const mappingYaml = sourceEl.querySelector('.acb-mapping').value;
      const sampleCsv = sourceEl._sampleCsv || null;
      if (!files.length && !sampleCsv) continue;
      if (files.length) {
        for (const file of files) {
          sources.push({
            csv_text: await file.text(),
            mapping_yaml_text: mappingYaml,
            mapping_name: broker,
            account_number: accountNumber,
          });
        }
      } else {
        sources.push({
          csv_text: sampleCsv,
          mapping_yaml_text: mappingYaml,
          mapping_name: broker,
          account_number: accountNumber,
        });
      }
    }

    if (!sources.length) throw new Error('Please upload at least one CSV file.');

    const fxFileEl = document.getElementById('acb-fx-file');
    const fxText = fxFileEl.files[0] ? await fxFileEl.files[0].text() : null;
    const start = document.getElementById('acb-start').value || null;
    const end = document.getElementById('acb-end').value || null;

    const resultJson = pyRunPipeline(JSON.stringify(sources), fxText, start, end);
    const result = JSON.parse(resultJson);

    if (result.warnings) {
      warningsEl.textContent = result.warnings;
      warningsEl.style.display = 'block';
    }

    if (result.error) {
      errorEl.textContent = result.error;
      errorEl.style.display = 'block';
      return;
    }

    renderResults(result.output_rows, result.holdings_rows);
    resultsEl.style.display = 'block';
    resultsEl.scrollIntoView({ behavior: 'smooth', block: 'start' });

  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.style.display = 'block';
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = 'Run';
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderResults(outputRows, holdingsRows) {
  renderTable('acb-transactions-table', outputRows, OUTPUT_COLS);
  renderTable('acb-holdings-table', holdingsRows, HOLDINGS_COLS);

  const downloadBtn = document.getElementById('acb-download');
  downloadBtn.style.display = '';
  downloadBtn.onclick = () => downloadCsv(buildCsv(outputRows, OUTPUT_COLS), 'acb_output.csv');
}

function colLabel(col) {
  return col.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function renderTable(id, rows, cols) {
  const el = document.getElementById(id);
  if (!rows.length) { el.innerHTML = '<p><em>No rows.</em></p>'; return; }

  const thead = `<tr>${cols.map(c => `<th>${colLabel(c)}</th>`).join('')}</tr>`;
  const tbody = rows
    .map(r => `<tr>${cols.map(c => `<td>${r[c] ?? ''}</td>`).join('')}</tr>`)
    .join('');
  el.innerHTML = `<table><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
}

function buildCsv(rows, cols) {
  const esc = v => {
    const s = String(v ?? '');
    return /[,"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [
    cols.join(','),
    ...rows.map(r => cols.map(c => esc(r[c])).join(',')),
  ].join('\n') + '\n';
}

function downloadCsv(text, filename) {
  const a = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(new Blob([text], { type: 'text/csv' })),
    download: filename,
  });
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  // Seed the form with one source row.
  const firstRow = buildSourceRow();
  document.getElementById('acb-sources').appendChild(firstRow);
  populateMappingTextarea(firstRow);

  document.getElementById('acb-add-source').addEventListener('click', addSource);
  document.getElementById('acb-load-sample').addEventListener('click', loadSampleData);
  document.getElementById('acb-run').addEventListener('click', handleRun);
  document.getElementById('acb-sources').addEventListener('click', e => {
    if (e.target.classList.contains('acb-remove-source')) {
      e.target.closest('.acb-source').remove();
      updateRemoveButtons();
    }
  });

  init();
});
