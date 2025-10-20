import os
from rq import Queue, Connection
from redis import Redis
from app import validate_file, EXPORTS_FOLDER

redis_url = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
redis_conn = Redis.from_url(redis_url)
q = Queue('default', connection=redis_conn)

# Example enqueue call (run in a separate process or from a deployed worker):
# job = q.enqueue(validate_file, 'app/uploads/qachecklistfusetest.pdf')
# print('Enqueued job', job.id)

if __name__ == '__main__':
    print('Worker script for enqueuing validation jobs. Use rq worker to run jobs:')
    print('rq worker -u', redis_url)
