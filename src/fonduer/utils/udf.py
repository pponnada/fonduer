import logging
from multiprocessing import JoinableQueue, Manager, Pool, Process
from queue import Empty

from fonduer.meta import Meta, new_sessionmaker

try:
    from IPython import get_ipython

    if "IPKernelApp" not in get_ipython().config:
        raise ImportError("console")
except (AttributeError, ImportError):
    from tqdm import tqdm
else:
    from tqdm import tqdm_notebook as tqdm


QUEUE_TIMEOUT = 3

# Grab pointer to global metadata
_meta = Meta.init()


def async_fill_input_queue(arg_tuple):
    input_queue = arg_tuple[0]
    preprocessor = arg_tuple[1]
    doc_index = arg_tuple[2]
    for doc_tuple in preprocessor.parse_by_index(doc_index):
        input_queue.put(doc_tuple)


class UDFRunner(object):
    """
    Class to run UDFs in parallel using simple queue-based multiprocessing
    setup.
    """

    def __init__(self, session, udf_class, parallelism=1, **udf_init_kwargs):
        self.logger = logging.getLogger(__name__)
        self.udf_class = udf_class
        self.udf_init_kwargs = udf_init_kwargs
        self.udfs = []
        self.pb = None
        self.session = session
        self.parallelism = parallelism

    def apply(self, xs, clear=True, parallelism=None, progress_bar=True, **kwargs):
        """
        Apply the given UDF to the set of objects xs, either single or
        multi-threaded, and optionally calling clear() first.
        """
        # Clear everything downstream of this UDF if requested
        if clear:
            self.clear(**kwargs)

        # Execute the UDF
        self.logger.info("Running UDF...")

        # Setup progress bar
        if progress_bar and hasattr(xs, "__len__"):
            self.logger.debug("Setting up progress bar...")
            self.pb = tqdm(total=len(xs))

        # Use the parallelism of the class if none is provided to apply
        parallelism = parallelism if parallelism else self.parallelism

        if parallelism < 2:
            self.apply_st(xs, clear=clear, **kwargs)
        else:
            self.apply_mt(xs, parallelism, clear=clear, **kwargs)

        # Close progress bar
        if self.pb is not None:
            self.logger.debug("Closing progress bar...")
            self.pb.close()

    def clear(self, **kwargs):
        raise NotImplementedError()

    def apply_st(self, xs, **kwargs):
        """Run the UDF single-threaded, optionally with progress bar"""
        udf = self.udf_class(**self.udf_init_kwargs)

        # Run single-thread
        for x in xs:
            if self.pb is not None:
                self.pb.update(1)

            udf.session.add_all(y for y in udf.apply(x, **kwargs))

        # Commit session and close progress bar if applicable
        udf.session.commit()

    def apply_mt(self, xs, parallelism, **kwargs):
        """Run the UDF multi-threaded using python multiprocessing"""
        if not _meta.postgres:
            raise ValueError("Fonduer must use PostgreSQL as a database backend.")

        # Create a Queue to feed documents to parsers
        manager = Manager()
        in_queue = manager.Queue()

        # Use an output queue to track multiprocess progress
        out_queue = JoinableQueue()

        total_count = len(xs)

        # Start UDF Processes
        for i in range(parallelism):
            udf = self.udf_class(
                in_queue=in_queue,
                out_queue=out_queue,
                worker_id=i,
                **self.udf_init_kwargs
            )
            udf.apply_kwargs = kwargs
            self.udfs.append(udf)

        # Start the UDF processes, and then join on their completion
        for udf in self.udfs:
            udf.start()

        # Fill input queue with documents
        pool = Pool(parallelism)
        in_tuples = ((in_queue, xs, index) for index in range(total_count))
        pool.map_async(func=async_fill_input_queue, iterable=in_tuples)

        count_parsed = 0
        while count_parsed < total_count:
            y = out_queue.get()
            # Update progress bar whenever an item is processed
            if y == UDF.TASK_DONE:
                count_parsed += 1
                if self.pb is not None:
                    self.pb.update(1)
            else:
                raise ValueError("Got non-sentinal output.")

        pool.close()
        pool.join()
        in_queue.put(UDF.QUEUE_CLOSED)

        for udf in self.udfs:
            udf.join()

        # Terminate and flush the processes
        for udf in self.udfs:
            udf.terminate()
        self.udfs = []


class UDF(Process):
    TASK_DONE = "done"
    QUEUE_CLOSED = "QUEUECLOSED"

    def __init__(self, in_queue=None, out_queue=None, worker_id=0):
        """
        in_queue: A Queue of input objects to process; primarily for running in parallel
        """
        Process.__init__(self)
        self.daemon = True
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.worker_id = worker_id

        # Each UDF starts its own Engine
        # See SQLalchemy, using connection pools with multiprocessing.
        Session = new_sessionmaker()
        self.session = Session()

        # We use a workaround to pass in the apply kwargs
        self.apply_kwargs = {}

    def run(self):
        """
        This method is called when the UDF is run as a Process in a
        multiprocess setting The basic routine is: get from JoinableQueue,
        apply, put / add outputs, loop
        """
        while True:
            try:
                x = self.in_queue.get(True, QUEUE_TIMEOUT)
                if x == UDF.QUEUE_CLOSED:
                    self.in_queue.put(UDF.QUEUE_CLOSED)
                    break
                self.session.add_all(y for y in self.apply(x, **self.apply_kwargs))
                self.out_queue.put(UDF.TASK_DONE)
            except Empty:
                continue
        self.session.commit()
        self.session.close()

    def apply(self, x, **kwargs):
        """This function takes in an object, and returns a generator / set / list"""
        raise NotImplementedError()
