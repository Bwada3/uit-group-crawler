"""One process owns the saved Facebook profile, including benchmark runs."""
import os


class ProfileLease:
    def __init__(self, directory):
        self.directory = directory
        self.file = None

    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.file=(self.directory/'.grouppulse.lock').open('a+b')
        self.file.seek(0,2)
        if not self.file.tell():
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file=None
            raise RuntimeError('Phiên Facebook đang được một server hoặc benchmark khác sử dụng.') from None
        return self

    def __exit__(self,*args):
        if self.file:
            self.file.close()
            self.file=None
