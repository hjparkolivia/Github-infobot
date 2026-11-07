import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import budget

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
ENV = {'GITHUB_TOKEN':'FIXTURE_SECRET', 'GITHUB_REPOSITORY':'owner/repo',
       'GITHUB_RUN_ID':'123', 'GITHUB_RUN_ATTEMPT':'1'}
PRICING = '''## Gemini 3.5 Flash-Lite  {% id="gemini-3.5-flash-lite" %}
{% tab title="Standard" %}
* Input price
* Free of charge
* $0.30 (text / image)
* Output price (including thinking tokens)
* Free of charge
* $2.50
{% /tab %}
{% tab title="Batch" %}
* Input price
* Free of charge
* $0.15
{% /tab %}
## Other Model
'''

def response(data=None, text=''):
    return Mock(text=text,json=Mock(return_value=data))

def setup(count=1, attempt=1, pricing=PRICING, rate=1358.73, date='2026-11-06'):
    session=Mock();session.headers={};session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
    session.get.side_effect=[response({'id':123,'run_attempt':attempt,'head_branch':'main','event':'workflow_dispatch','created_at':'2026-11-07T00:00:00Z'}),response({'total_count':count})]
    factory=Mock(return_value=session)
    external=Mock(side_effect=[response(text=pricing),response({'base':'USD','date':date,'rates':{'KRW':rate}})])
    return factory,external

class BudgetTests(unittest.TestCase):
    def decide(self, **kwargs):
        factory,external=setup(**kwargs)
        with patch.dict(os.environ,ENV,clear=True):
            result=budget.check_budget(budget.MODEL,now=NOW,session_factory=factory,external_get=external)
        return result,factory,external

    def test_allow_fiftieth_run_and_never_leak_github_auth_to_other_hosts(self):
        result,factory,external=self.decide(count=50)
        self.assertTrue(result.allowed,result.reason)
        self.assertIn('5,000/5,000',result.note)
        self.assertLessEqual(result.slots*budget.REQUEST_RESERVE_KRW,budget.MONTHLY_RESERVE_KRW)
        for call in external.call_args_list:self.assertNotIn('headers',call.kwargs)
        self.assertEqual(factory.return_value.headers['Authorization'],'Bearer FIXTURE_SECRET')

    def test_over_budget_blocks_before_price_or_fx_fetch(self):
        result,_,external=self.decide(count=51)
        self.assertFalse(result.allowed);external.assert_not_called()

    def test_rerun_is_blocked_even_if_same_run_id_not_counted_twice(self):
        factory,external=setup()
        with patch.dict(os.environ,{**ENV,'GITHUB_RUN_ATTEMPT':'2'},clear=True):
            result=budget.check_budget(budget.MODEL,now=NOW,session_factory=factory,external_get=external)
        self.assertFalse(result.allowed);factory.assert_not_called();external.assert_not_called()

    def test_no_history_means_no_paid_call(self):
        with patch.dict(os.environ,{},clear=True):self.assertFalse(budget.check_budget(budget.MODEL).allowed)
        result,_,_=self.decide(count=0);self.assertFalse(result.allowed)

    def test_expensive_model_override_cannot_bypass_budget(self):
        self.assertFalse(budget.check_budget('gemini-3.8-flash').allowed)

    def test_price_increase_or_unparseable_prices_stop_ai(self):
        for price in (PRICING.replace('$2.50','$20.00'),PRICING.replace('$0.30','$0.31'),'new undocumented format'):
            result,_,_=self.decide(pricing=price);self.assertFalse(result.allowed,result)

    def test_fx_ceiling_and_age_checks(self):
        for kwargs in ({'rate':2001},{'date':'2026-10-01'},{'date':'2026-11-08'},{'rate':0}):
            result,_,_=self.decide(**kwargs);self.assertFalse(result.allowed,result)

    def test_history_network_error_never_fails_open_or_discloses_token(self):
        factory,external=setup();factory.return_value.get.side_effect=RuntimeError('FIXTURE_SECRET')
        with patch.dict(os.environ,ENV,clear=True):
            result=budget.check_budget(budget.MODEL,now=NOW,session_factory=factory,external_get=external)
        self.assertFalse(result.allowed);self.assertNotIn('FIXTURE_SECRET',result.reason)

    def test_first_attempt_verified_at_server_as_well_as_environment(self):
        result,_,_=self.decide(attempt=2);self.assertFalse(result.allowed)

    def test_kst_month_boundary_is_not_machine_timezone(self):
        value=datetime(2026,10,31,15,1,tzinfo=timezone.utc)
        self.assertEqual(budget.month_window(value),'2026-10-31T15:00:00Z..2026-11-30T14:59:59Z')

    def test_exclusive_paid_claim_prevents_second_call_in_one_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            decision=budget.Decision(True,'verified',1,'123')
            budget.claim_once(decision,folder)
            with self.assertRaises(FileExistsError):budget.claim_once(decision,folder)
            self.assertEqual(len(list(Path(folder).glob('*.json'))),1)

    def test_payload_bounds_include_multibyte_readmes_and_keep_ids(self):
        repos=[{'id':i,'full_name':f'owner/tool-{i}','description':'설명'*1000,'topics':['topic']*20,'excerpt':'漢字'*10000} for i in range(12)]
        payload=budget.payload_for(repos)
        self.assertLessEqual(len(payload.encode('utf-8')),16000)
        self.assertEqual({x['id'] for x in json.loads(payload)},set(range(12)))

    def test_standard_pricing_not_cheaper_batch_or_different_model(self):
        self.assertEqual(tuple(map(str,budget.standard_prices(PRICING))),('0.30','2.50'))

if __name__=='__main__':unittest.main()
