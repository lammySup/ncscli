#!/usr/bin/env python3
"""
Command-line interface for Neocortix Scalable Compute
"""
# standard library modules
import argparse
import collections
from concurrent import futures
import json
import logging
import os
import sys
import random
import time
import uuid

# third-party modules
import requests

__version__ = '0.0.3'
logger = logging.getLogger(__name__)


def ncscReqHeaders( authToken ):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Neocortix-Cloud-API-Version": "1",
        "X-Neocortix-Cloud-API-AuthToken": authToken
    }

def queryNcsSc( urlTail, authToken, reqParams=None, maxRetries=1 ):
    #if random.random() > .75:
    #    raise requests.exceptions.RequestException( 'simulated exception' )
    headers = ncscReqHeaders( authToken )
    url = 'https://cloud.neocortix.com/cloud-api/sc/' + urlTail
    # jsonize the reqParams, but only if it's not a string (to avoid jsonizing if already json)
    if not isinstance( reqParams, str ):
        reqParams = json.dumps( reqParams )
    resp = requests.get( url, headers=headers, data=reqParams )
    if (resp.status_code < 200) or (resp.status_code >= 300):
        logger.warning( 'error code from server (%s) %s', resp.status_code, resp.text )
        logger.info( 'error url "%s"', url )
        if True:  # resp.status_code in [500, 502, 504]:
            if maxRetries > 0:
                time.sleep( 10 )
                return queryNcsSc( urlTail, authToken, reqParams, maxRetries-1 )
    try:
        content = resp.json()
    except Exception:
        content = {}
    return { 'content': content, 'statusCode': resp.status_code }

def getAppVersions( authToken ):
    response = queryNcsSc( 'info/mobile-app-versions', authToken)
    respContent = response['content']
    versions = [x['value'] for x in respContent ]
    logger.debug( 'appVersions: %s', versions )
    return versions

def launchNcscInstances( authToken, numReq=1,
        regions=[], abis=[], sshClientKeyName=None, jsonFilter=None ):
    appVersions = getAppVersions( authToken )
    if not appVersions:
        # something is very wrong
        logger.error( 'could got get AppVersions from server')
        return {}
    minAppVersion = 1623
    latestVersion = max( appVersions )

    headers = ncscReqHeaders( authToken )

    reqId = str( uuid.uuid4() )

    reqData = {
        #"user":"hacky.sack@gmail.com",
        #'mobile-app-versions': [minAppVersion, latestVersion],
        'abis': abis,
        'job': reqId,
        'regions': regions,
        'ssh_key': sshClientKeyName,
        'count': numReq
        }
    if jsonFilter:
        try:
            filters = json.loads( jsonFilter )
        except Exception:
            logger.error( 'invalid json in filter "%s"', jsonFilter )
            raise
        else:
            if filters:
                if not isinstance( filters, dict ):
                    logger.error( 'json in filter is not a dict "%s"', jsonFilter )
                    raise TypeError('json in filter is not a dict')
                reqData.update( filters )
    reqDataStr = json.dumps( reqData )

    logger.info( 'reqData: %s', reqDataStr )
    url = 'https://cloud.neocortix.com/cloud-api/sc/instances'
    #logger.info( 'posting with auth %s', authToken )
    resp = requests.post( url, headers=headers, data=reqDataStr )
    logger.info( 'response code %s', resp.status_code )
    if (resp.status_code < 200) or (resp.status_code >= 300):
        logger.warning( 'error code from server (%s) %s', resp.status_code, resp.text )
        # try recovering by retrieving list of instances that match job id
        logger.info( 'attempting recovery from launch error' )
        time.sleep( 30 )
        listReqData = {
            'job': reqId,
            }
        try:
            resp2 = queryNcsSc( 'instances', authToken, maxRetries=20,
                reqParams=json.dumps(listReqData))
        except Exception as exc:
            logger.error( 'exception getting list of instances (%s) "%s"',
                type(exc), exc )
            # in case of excption, return the original error code
            return {'serverError': resp.status_code, 'reqId': reqId}
        else:
            if (resp2['statusCode'] < 200) or (resp2['statusCode'] >= 300):
                # in case of persistent error, return the last error code
                logger.info( 'returning server error')
                return {'serverError': resp2['statusCode'], 'reqId': reqId}
            else:
                logger.info( 'returning resp2 content %s', resp2['content'].keys() )
                return resp2['content']['my']
    return resp.json()

def terminateNcscInstance( authToken, iid ):
    headers = ncscReqHeaders( authToken )
    url = 'https://cloud.neocortix.com/cloud-api/sc/instances/' + iid
    #logger.debug( 'deleting instance %s', iid )
    resp = requests.delete( url, headers=headers )
    if (resp.status_code < 200) or (resp.status_code >= 300):
        logger.warn( 'response code %s', resp.status_code )
        if len( resp.text ):
            logger.info( 'response "%s"', resp.text )
        if resp.status_code == 502:  # "bad gateway"
            time.sleep( 10 )
            return terminateNcscInstance( authToken, iid )
    return resp.status_code


def doCmdLaunch( args ):
    authToken = args.authToken

    instanceAbis = []
    instanceType = args.itype  # armeabi-v7a
    if instanceType:
        logger.info( 'requested instance type "%s" (might work)', instanceType)
        instanceAbis = [instanceType]

    instances = []
    try:
        infos = launchNcscInstances( authToken, args.count, 
            sshClientKeyName=args.sshClientKeyName,
            regions=args.region, abis=instanceAbis, jsonFilter=args.filter )
        if 'serverError' in infos:
            logger.warning( 'got serverError %d', infos['serverError'])
            return infos['serverError']
    except Exception as exc:
        logger.error( 'exception launching instances (%s) "%s"',
            type(exc), exc, exc_info=True )
        return 13  # error 13
    # regions=['russia-ukraine-belarus']  abis=['arm64-v8a']
    for info in infos:
        instances.append( info )

    # collect the ID of the created instances
    iids = []
    for inst in instances:
        iids.append( inst['id'] )
        #logger.debug( 'created instance %s', inst['id'] )
    logger.info( 'allocated %d instances', len(iids) )

    reqParams = {"show-device-info":True}
    startedInstances = {}
    # wait while instances are still starting, but with a timeout
    timeLimit = 600 # seconds
    deadline = time.time() + timeLimit
    startedSet = set()
    failedSet = set()
    try:
        while True:
            starting = False
            launcherStates = collections.Counter()
            for iid in iids:
                if iid in startedSet:
                    continue
                try:
                    details = queryNcsSc( 'instances/%s' % iid, authToken, reqParams )['content']
                except Exception as exc:
                    logger.warning( 'exception checking instance state (%s) "%s"',
                        type(exc), exc )
                    continue
                if 'state' not in details:
                    logger.warning( 'no "state" in content of response (%s)', details )
                    continue
                iState = details['state']
                launcherStates[ iState ] += 1
                if details['state'] == 'started':
                    startedSet.add( iid )
                    startedInstances[ iid ] = details
                if details['state'] == 'failed':
                    failedSet.add( iid )
                if details['state'] == 'initial':
                    logger.info( '%s %s', details['state'], iid )
                #if details['state'] in ['initial', 'starting']:
                if details['state'] != 'started':
                    starting = True
                    #logger.debug( '%s %s', details['state'], iid )
            logger.info( '%d instance(s) launched so far; %s',
                len( startedSet ), launcherStates )
            if not starting:
                break
            if time.time() > deadline:
                logger.warning( 'took too long for some instances to start' )
                break
            time.sleep( 5 )
    except KeyboardInterrupt:
        logger.info( 'caught SIGINT (ctrl-c), skipping ahead' )

    #nStillStarting = len(iids) - (len(startedSet) + len(failedSet))
    logger.info( 'started %d Instances; %s',
        len(startedSet), launcherStates )

    logger.info( 'querying for device-info')
    # print details of created instances to stdout
    if args.json:
        print( '[')
        jsonFirstElem=True
    for iid in iids:
        try:
            #reqParams = {"show-device-info":True}
            if iid in startedInstances:
                #logger.debug( 'reusing instance info')
                details = startedInstances[iid]
            else:
                logger.info( 're-querying instance info')
                details = queryNcsSc( 'instances/%s' % iid, authToken, reqParams )['content']
        except Exception as exc:
            logger.error( 'exception getting instance details (%s) "%s"',
                type(exc), exc )
            continue
        except KeyboardInterrupt:
            logger.info( 'caught SIGINT (ctrl-c), skipping ahead' )
            break
        #logger.debug( 'NCSC Inst details %s', details )
        if args.json:
            outRec = details.copy()
            outRec['instanceId'] = iid
            if jsonFirstElem:
                jsonFirstElem = False
            else:
                print( ',', end=' ')
            print( json.dumps( outRec ) )
        else:
            print( "%s,%s,%s" % (iid, details['state'], details['job']) )
    if args.json:
        print( ']')
    logger.info( 'finished')
    return 0 # no err

def doCmdList( args ):
    authToken = args.authToken

    if args.instanceId and (args.instanceId != ['ALL']):
        iids = args.instanceId
    else:
        try:
            response = queryNcsSc( 'instances', authToken)
        except Exception as exc:
            logger.error( 'exception getting list of instances (%s) "%s"',
                type(exc), exc )
            return
        instancesJson = response['content']
        logger.debug( 'response %s', instancesJson )
        if 'running' in instancesJson:
            runningInstances = instancesJson['my'] # 'running'
        else:
            runningInstances = []
        logger.info( 'found %d allocated instances', len( runningInstances ) )
        iids = [inst['id'] for inst in runningInstances]

    if args.json:
        print( '[')
        jsonFirstElem=True
    for iid in iids:
        try:
            reqParams = {"show-device-info":True}
            response = queryNcsSc( 'instances/%s' % iid, authToken, reqParams )
        except Exception as exc:
            logger.error( 'exception getting instance details (%s) "%s"',
                type(exc), exc )
            continue
        respCode = response['statusCode']
        if (respCode < 200) or (respCode >= 300):
            logger.warning( 'instanceId %s not found', iid)
            continue
        details = response['content']
        instState = details['state']
        #logger.info( 'NCSC Inst details %s', details )
        if 'app-version' in details:
            logger.info( 'iid: %s version: %s', iid, details['app-version']['code'] )
        #if 'ram' in details:
        #    logger.info( 'ram %.1f M (tot); storage %.1f M (free); cores %d', details['ram']['total']/1000000,
        #        details['storage']['free']/1000000, len( details['cpu']['cores'] ) )
        #else:
        #    logger.warning( 'no "ram" listed for inst %s (which was %s)', iid, details['state']  )
        #if 'events' in details:
        #    logger.info( 'state: %s, events: %s', instState, details['events'] )                
        #else:
        #    logger.warning( 'no "events" listed for inst %s (which was %s)', iid, details['state']  )
        if 'failure' in details:
            logger.warning( 'failure: %s', details['failure'] )                

        if 'progress' in details:
            if instState != 'started' or 'SC instance launched' not in details['progress']:
                logger.warning( '"progress": %s', details['progress'] )                
        #else:
        #    logger.warning( 'no "progress" listed for inst %s (which was %s)', iid, details['state']  )

        if args.json:
            outRec = details.copy()
            outRec['instanceId'] = iid
            if (not args.showPasswords) and ('ssh' in outRec):
                outRec['ssh']['password'] = '*'
            if jsonFirstElem:
                jsonFirstElem = False
            else:
                print( ',', end=' ')
            print( json.dumps( outRec ) )
        else:
            port = details['ssh']['port'] if 'ssh' in details else 0
            host = details['ssh']['host'] if 'ssh' in details else 'None'
            pw = details['ssh']['password'] if 'ssh' in details else ''
            jobId = details['job']
            if not args.showPasswords:
                pw = '*'
            print( '%s,%s,%d,%s,%s,%s' % ( iid, details['state'], port, host, pw, jobId ) )
            #print( '%s,"%s",%s,%d,%s,%s' % ( iid, inst['name'], details['state'], port, host, pw ) )
            #print( iid, inst['name'], details['state'], port, host, sep=',' )
    if args.json:
        print( ']')

def terminateInstances( authToken, instanceIds ):
    def terminateOne( iid ):
        logger.info( 'terminating %s', iid )
        terminateNcscInstance( authToken, iid )
    if instanceIds and (len(instanceIds) >0) and (isinstance(instanceIds[0], str )):
        nWorkers = 2
        with futures.ThreadPoolExecutor( max_workers=nWorkers ) as executor:
            parIter = executor.map( terminateOne, instanceIds )
            parResultList = list( parIter )
    
def doCmdTerminate( args ):
    authToken = args.authToken

    def terminateOne( iid ):
        logger.info( 'terminating %s', iid )
        #print( 'terminating', iid )
        terminateNcscInstance( authToken, iid )

    startTime = time.time()
    threading = True
    if args.instanceId == ['ALL']:
        try:
            response = queryNcsSc( 'instances', authToken)
        except Exception as exc:
            logger.error( 'exception getting list of instances (%s) "%s"',
                type(exc), exc )
            return
        logger.info( 'response content %s', response['content'].keys() )
        instancesJson, respCode = (response['content'], response['statusCode'] )

        runningInstances = instancesJson['my']  # 'running'
        #logger.info( 'runningInstances %s', runningInstances )
        logger.info( 'found %d running instances', len( runningInstances ) )
        if threading:
            runningIids = [inst['id'] for inst in runningInstances]
            #logger.info( 'runningIids %s', runningIids )
            terminateInstances( authToken, runningIids )
        else:
            for inst in runningInstances:
                iid = inst['id']
                logger.info( 'terminating %s "%s"', iid, inst['name'] )
                terminateNcscInstance( authToken, iid )
    elif not args.instanceId:
        logger.error( 'no instance ID provided for terminate' )
    else:
        if threading:
            terminateInstances( authToken, args.instanceId )
        else:
            for iid in args.instanceId:
                logger.info( 'terminating %s', iid )
                terminateNcscInstance( authToken, iid )
    logger.info( 'took %.1f seconds', time.time() - startTime)

if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s %(levelname)s %(module)s %(funcName)s %(message)s', datefmt='%Y/%m/%d %H:%M:%S')
    logger.setLevel(logging.INFO)
    logger.debug( 'the logger is configured' )

    ap = argparse.ArgumentParser( description=__doc__, fromfile_prefix_chars='@',
        #formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
    ap.add_argument( 'subcommand', help='subcommand (only "sc" is allowed)' )
    ap.add_argument( 'action', help='the action to perform', 
        choices=['launch', 'list', 'terminate']
        )
    ap.add_argument('--version', action='version', version=__version__)
    #ap.add_argument('--verbose', '-v', action='count', default=0)
    ap.add_argument( '--count', type=int, default=1, help='the number of instances required (default=1)' )
    ap.add_argument( '--instanceId', type=str, nargs='+', help='one or more instance IDs (or ALL to terminate all)' )
    ap.add_argument( '--filter', help='json to filter instances for launch (or maybe list)' )
    ap.add_argument( '--json', action='store_true', help='for json-format output' )
    ap.add_argument( '--region', nargs='+', help='the geographic region(s) to target' )
    ap.add_argument( '--showPasswords', action='store_true', help='if you want launch or list to show passwords' )
    ap.add_argument( '--sshClientKeyName', help='the name of the uploaded ssh client key to use' )
    ap.add_argument( '--itype', default=None, help='the instance type to create' )
    ap.add_argument( '--authToken', type=str, default=None,
        help='the NCS authorization token to use' )
    args = ap.parse_args()

    #logger.info( 'args %s', args ) # be careful not to leak authToken
    
    if args.authToken == None:
        tok = os.getenv( 'NCS_AUTH_TOKEN' )
        if tok:
            args.authToken = tok
        else:
            sys.exit( 'no authToken found' )

    if args.subcommand != 'sc':
        sys.exit( 'sc is the only available subcommand')

    if args.action == 'launch':
        exitCode = doCmdLaunch( args )
        if exitCode > 400:
            exitCode -= 400
        sys.exit( exitCode )
    elif args.action == 'list':
        doCmdList( args )
    elif args.action == 'terminate':
        doCmdTerminate( args )
    else:
        sys.exit( 'unrecognized action %s' % (args.action) )
       
