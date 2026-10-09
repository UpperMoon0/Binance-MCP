"""Owner-run post-deployment diagnostics. Only passive MCP calls, never activation."""
import argparse
import asyncio
import json
import os
import httpx

EXPECTED = {'binance_readiness', 'binance_execution_status', 'binance_trade_preview',
            'binance_fee_compatibility', 'binance_portfolio_snapshot', 'binance_market_scan'}


async def check(url):
    headers = {'Accept':'application/json, text/event-stream'}
    if os.getenv('MCP_READINESS_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['MCP_READINESS_TOKEN']
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async def rpc(method, params):
            response = await client.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
            response.raise_for_status()
            data=response.json()
            if 'error' in data: raise RuntimeError('MCP protocol error')
            return data['result']
        listed=await rpc('tools/list',{})
        tools={t['name']:t for t in listed['tools']}
        missing=sorted(EXPECTED-tools.keys())
        if missing:
            return {'pass':False,'blocker':'STALE_OR_INCOMPLETE_DISCOVERY','missingTools':missing}
        if any(tools[n].get('annotations',{}).get('readOnlyHint') is not True for n in EXPECTED):
            return {'pass':False,'blocker':'PASSIVE_CONTRACT_MISMATCH'}
        result=await rpc('tools/call',{'name':'binance_readiness','arguments':{'checkExchange':True}})
        if result.get('isError'):
            return {'pass':False,'error':result.get('structuredContent',{}).get('error',{'blocker':'MCP_CALL_FAILED'})}
        value=result['structuredContent']
        observation=value['research']['observations']
        research_ok=observation['portfolio']['complete'] and 'market' in observation
        return {'pass':research_ok,'identity':value['identity'],'researchScopeComplete':observation['portfolio']['complete'],
                'marketChecked':'market' in observation,'excludedCoverage':observation['portfolio']['excludedCoverage'],
                'provisioned':value['provisioned'],'paper':value['paper'],'protectedLive':value['protectedLive'],
                'financialWrites':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Read-only MCP discovery/readiness smoke check; token from MCP_READINESS_TOKEN.')
    parser.add_argument('mcp_url')
    args=parser.parse_args()
    try:
        result=asyncio.run(check(args.mcp_url))
    except Exception:
        result={'pass':False,'blocker':'CONNECTOR_TRANSPORT_OR_AUTH_FAILED'}
    print(json.dumps(result))
    raise SystemExit(0 if result['pass'] else 1)
