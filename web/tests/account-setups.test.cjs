const test = require('node:test');
const assert = require('node:assert/strict');
const {validShape, parseSaved, validate, storageUnchanged} = require('../static/account-setups.js');

const setup = {id: 'sample', name: 'Trend research', account: 'bot:alpha', strategy: 'Trend', exchange: 'kraken', budget: 1000, drawdown: 15};
const research = {accounts: [{id: 'bot:alpha', label: 'alpha'}], strategies: [{name: 'Trend', venues: ['kraken']}]};

test('stored choices exclude observations and unknown fields', () => {
  assert.equal(validShape(setup), true);
  assert.equal(validShape({...setup, balance: 500}), false);
  assert.equal(validShape({...setup, token: 'secret'}), false);
  assert.equal(validShape({...setup, name: ''}), false);
  assert.equal(validShape({...setup, name: 'a'.repeat(81)}), false);
  assert.equal(validShape({...setup, budget: '1000'}), false);
});

test('storage corruption is rejected rather than treated as empty', () => {
  assert.deepEqual(parseSaved(null), []);
  assert.deepEqual(parseSaved(JSON.stringify([setup])), [setup]);
  for (const raw of ['oops', '{}', 'null', '[null]', JSON.stringify([setup, setup])]) {
    assert.throws(() => parseSaved(raw));
  }
});

test('obsolete choices remain readable but cannot be saved until corrected', () => {
  const old = {...setup, account: 'bot:removed', strategy: 'Removed', exchange: 'old'};
  assert.deepEqual(parseSaved(JSON.stringify([old])), [old]);
  assert.match(validate(old, research), /strategy/);
  assert.match(validate({...setup, exchange: 'old'}, research), /venue/);
  assert.match(validate({...setup, account: 'wallet:removed'}, research), /account/);
  assert.equal(validate({...setup, account: ''}, research), '');
  assert.equal(validate(setup, research), '');
});

test('numeric preferences reject missing, zero, negative and non-finite values', () => {
  for (const budget of [NaN, Infinity, -Infinity, 0, -5]) {
    assert.match(validate({...setup, budget}, research), /budget/);
    assert.equal(validShape({...setup, budget}), false);
  }
  for (const drawdown of [NaN, Infinity, -Infinity, 0, -5, 100.1]) {
    assert.match(validate({...setup, drawdown}, research), /drawdown/);
    assert.equal(validShape({...setup, drawdown}), false);
  }
  assert.equal(validate({...setup, budget: 0.01, drawdown: 100}, research), '');
});

test('an unobserved venue cannot be invented for an untested strategy', () => {
  const noVenues = {...research, strategies: [{name: 'Trend', venues: []}]};
  assert.match(validate(setup, noVenues), /venue/);
});

// An unread store must never be mistaken for an empty store after access recovers.
test('writes require the successfully loaded storage snapshot to remain current', () => {
  assert.equal(storageUnchanged(undefined, null), false);
  assert.equal(storageUnchanged(undefined, JSON.stringify([setup])), false);
  assert.equal(storageUnchanged(null, null), true);
  assert.equal(storageUnchanged(null, JSON.stringify([setup])), false);
  assert.equal(storageUnchanged(JSON.stringify([setup]), null), false);
  assert.equal(storageUnchanged(JSON.stringify([setup]), JSON.stringify([setup])), true);
  assert.equal(storageUnchanged('malformed', JSON.stringify([setup])), false);
  assert.equal(storageUnchanged('malformed', 'malformed'), true);
});
