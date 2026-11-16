BRIDGE = r"""
import base64,json,sys,psycopg
from datetime import datetime,timezone,timedelta
init=json.loads(sys.stdin.readline())
assert init['type']=='init' and init['credentials'] is None
token=init['token']
def emit(kind,identifier=None,**fields):
    print(json.dumps(dict(type=kind,id=identifier or kind,token=token,**fields)),flush=True)
emit('qr',value=base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode(),
     expires_at=(datetime.now(timezone.utc)+timedelta(seconds=100)).isoformat())
emit('creds_set',creds='opaque-session')
sent=False
for line in sys.stdin:
    frame=json.loads(line)
    assert frame['token']==token
    match frame['type']:
        case 'result':
            assert frame['request_id']=='creds_set' and frame['ok']
            with psycopg.connect(sys.argv[1]) as db:
                sealed=db.execute('SELECT ciphertext FROM zalo_personal_auth').fetchone()[0]
                assert b'opaque-session' not in sealed
            emit('identity',account_id='123456789')
            emit('ready')
            if sys.argv[2]=='messages':
                message=dict(message_id='incoming-1',sender_id='987654321',thread_id='987654321',
                             from_me=False,timestamp=int(datetime.now(timezone.utc).timestamp()),text='hello')
                emit('message','unbound',**dict(message,message_id='unbound-1'))
                emit('message','original',**message)
                emit('message','replay',**message)
        case 'send':
            assert not sent and frame['thread_id']=='987654321' and frame['text']=='reply'
            with psycopg.connect(sys.argv[1]) as db:
                row=db.execute('SELECT state,text FROM zalo_personal_sends WHERE command_id=%s',
                               (frame['command_id'],)).fetchone()
                assert row==('dispatch_started','reply')
            sent=True
            emit('send_result',command_id=frame['command_id'],message_id='sdk-message',outcome='accepted')
        case 'shutdown':
            emit('closed',reason='shutdown')
            break
        case 'lease':
            assert frame['lease_ms']==30000
        case 'typing':
            assert frame['thread_id']=='987654321'
        case _:
            raise AssertionError(frame['type'])
"""
