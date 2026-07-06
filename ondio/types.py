from dataclasses import dataclass

@dataclass(frozen=True)
class FlacHeader:
    """ STREAMINFO metadata of a FLAC file/stream. """

    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int
    md5: bytes
    audio_data_offset: int

    @property
    def duration(self) -> float:
        """Stream duration in seconds (0.0 if sample_rate is 0)."""
        if self.sample_rate == 0:
            return 0.0
        return self.total_samples / self.sample_rate
