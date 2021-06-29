from __future__ import print_function

import itertools
import collections
import json
import json5
import os
import re
import sys
import time

import multiprocessing as mp
from time import strftime

from intern.remote.boss import BossRemote
from intern.resource.boss.resource import ChannelResource, ExperimentResource, CoordinateFrameResource
from .secrets import boss_credentials

from . import lib
from .cacheservice import CacheService
from . import exceptions 
from .lib import ( 
  colorize, red, mkdir, Vec, Bbox,  
  jsonify, generate_slices,
  generate_random_string
)
from .meshservice import PrecomputedMeshService
from .provenance import DataLayerProvenance
from .skeletonservice import PrecomputedSkeletonService
from .storage import SimpleStorage, Storage, reset_connection_pools
from . import txrx
from .volumecutout import VolumeCutout
from . import sharedmemory

# Set the interpreter bool
try:
  INTERACTIVE = bool(sys.ps1)
except AttributeError:
  INTERACTIVE = bool(sys.flags.interactive)

REGISTERED_PLUGINS = {}
def register_plugin(key, creation_function):
  REGISTERED_PLUGINS[key.lower()] = creation_function

class SharedConfiguration(object):
  """
  CloudVolume represents an interface to a dataset layer at a given
  mip level. You can use it to send and receive data from neuroglancer
  datasets on supported hosts like Google Storage, S3, and local Filesystems.

  Uploading to and downloading from a neuroglancer dataset requires specifying
  an `info` file located at the root of a data layer. Amongst other things, 
  the bounds of the volume are described in the info file via a 3D "offset" 
  and 3D "shape" in voxels.

  Required:
    cloudpath: Path to the dataset layer. This should match storage's supported
      providers.

      e.g. Google: gs://neuroglancer/$DATASET/$LAYER/
           S3    : s3://neuroglancer/$DATASET/$LAYER/
           Lcl FS: file:///tmp/$DATASET/$LAYER/
           Boss  : boss://$COLLECTION/$EXPERIMENT/$CHANNEL
  Optional:
    mip: (int or iterable) Which level of downsampling to read to/write from.
        0 is the highest resolution. Can also specify the voxel resolution as
        an iterable. 
    bounded: (bool) If a region outside of volume bounds is accessed:
        True: Throw an error
        False: Fill the region with black (useful for e.g. marching cubes's 1px boundary)
    autocrop: (bool) If the uploaded or downloaded region exceeds bounds, process only the
      area contained in bounds. Only has effect when bounded=True.
    fill_missing: (bool) If a file inside volume bounds is unable to be fetched:
        True: Use a block of zeros
        False: Throw an error
    cache: (bool or str) Store downloaded and uploaded files in a cache on disk 
      and preferentially read from it before redownloading. 
        - falsey value: no caching will occur.
        - True: cache will be located in a standard location.
        - non-empty string: cache is located at this file path
    compress_cache: (None or bool) If not None, override default compression behavior for the cache.
    cdn_cache: (int, bool, or str) Sets the Cache-Control HTTP header on uploaded image files.
      Most cloud providers perform some kind of caching. As of this writing, Google defaults to
      3600 seconds. Most of the time you'll want to go with the default. 
      - int: number of seconds for cache to be considered fresh (max-age)
      - bool: True: max-age=3600, False: no-cache
      - str: set the header manually
    info: (dict) in lieu of fetching a neuroglancer info file, use this provided one.
            This is useful when creating new datasets.
    parallel (int: 1, bool): number of extra processes to launch, 1 means only use the main process. If parallel is True
      use the number of CPUs returned by multiprocessing.cpu_count()
    output_to_shared_memory (deprecated, bool: False, str): Write results to shared memory. Don't make copies from this buffer
      and don't automatically unlink it. If a string is provided, use that shared memory location rather than
      the default. Please use vol.download_to_shared_memory(slices_or_bbox) instead.
    provenance: (string, dict, or object) in lieu of fetching a neuroglancer provenance file, use this provided one.
            This is useful when doing multiprocessing.
    progress: (bool) Show tqdm progress bars. 
        Defaults True in interactive python, False in script execution mode.
    compress: (bool, str, None) pick which compression method to use. 
      None: (default) Use the pre-programmed recommendation (e.g. gzip raw arrays and compressed_segmentation)
      bool: True=gzip, False=no compression, Overrides defaults
      str: 'gzip', extension so that we can add additional methods in the future like lz4 or zstd. 
        '' means no compression (same as False).
    non_aligned_writes: (bool) Enable non-aligned writes. Not multiprocessing safe without careful design.
      When not enabled, a ValueError is thrown for non-aligned writes.
    order: (str) whether the memory layout is fortran order or C order. We use fortran order in default 
      for consisitancy with neuroglancer precomputed. options: {'F', 'C'}
  """
  def __init__(self, cloudpath, mip=0, bounded=True, autocrop=False, fill_missing=False, 
      cache=False, compress_cache=None, cdn_cache=True, progress=INTERACTIVE, info=None, provenance=None, 
      compress=None, non_aligned_writes=False, parallel=1, output_to_shared_memory=False, order='F'):

    self.autocrop = bool(autocrop)
    self.bounded = bool(bounded)
    self.cdn_cache = cdn_cache
    self.compress = compress
    self.fill_missing = bool(fill_missing)
    self.non_aligned_writes = bool(non_aligned_writes)
    self.progress = bool(progress)
    self.path = lib.extract_path(cloudpath)
    self.shared_memory_id = self.generate_shared_memory_location()
    if type(output_to_shared_memory) == str:
      self.shared_memory_id = str(output_to_shared_memory)
    self.output_to_shared_memory = bool(output_to_shared_memory)

    if self.output_to_shared_memory:
      warn("output_to_shared_memory as an attribute is deprecated. Please use vol.download_to_shared_memory(slices_or_bbox) instead.")

    if type(parallel) == bool:
      parallel = mp.cpu_count() if parallel == True else 1
    elif parallel <= 0:
      raise ValueError('Number of processes must be >= 1. Got: ' + str(parallel))
    else:
      parallel = int(parallel)

    self.cdn_cache = cdn_cache
    self.compress = compress
    self.compress_level = compress_level
    self.green = bool(green)
    self.mip = mip
    self.parallel = parallel 
    self.progress = bool(progress)
    self.secrets = secrets
    self.order = order
    self.args = args
    self.kwargs = kwargs

    self.pid = os.getpid()

    # memory layout order
    if order == 'F':
      self.isfortran = True
    elif order == 'C':
      self.isfortran = False 
    else:
      raise NameError

  @classmethod
  def from_numpy(cls, arr, vol_path='file:///tmp/image/'+generate_random_string(),
                  resolution=(4,4,40), voxel_offset=(0,0,0), 
                  chunk_size=(128,128,64), layer_type=None):
    if re.match('^file://', vol_path):
      mkdir(vol_path)

    if not layer_type:
      if arr.dtype in (np.uint8, np.float32, np.float16, np.float64):
        layer_type = 'image'
      elif arr.dtype in (np.uint32, np.uint64, np.uint16):
        layer_type = 'segmentation'
      else:
        raise NotImplementedError

    if arr.ndim == 3:
      num_channels = 1
    elif arr.ndim == 4:
      num_channels = arr.shape[-1]
    else:
      raise NotImplementedError

    order = 'F' if np.isfortran(arr) else 'C'
    info = cls.create_new_info(num_channels, layer_type, arr.dtype.name, 'raw', resolution, voxel_offset, arr.shape[:3])
    vol = CloudVolume(vol_path, info=info, bounded=True, autocrop=False, order=order) 
    # save the info file
    vol.commit_info()
    vol.provenance.processing.append({
      'method': 'from_numpy',
      'date': strftime('%Y-%m-%d %H:%M %Z')
    })
    vol.commit_provenance()
    # save the numpy array
    vol[:,:,:] = arr
    return vol 

  def __setstate__(self, d):
    """Called when unpickling which is integral to multiprocessing."""
    self.__dict__ = d 

    if 'cache' in d:
      self.init_submodules(d['cache'].enabled)
    else:
      self.init_submodules(False)
    
    pid = os.getpid()
    if 'pid' in d and d['pid'] != pid:
      # otherwise the pickle might have references to old connections
      reset_connection_pools() 
      self.pid = pid
  
  def init_submodules(self, cache):
    """cache = path or bool"""
    self.cache = CacheService(cache, weakref.proxy(self)) 
    self.mesh = PrecomputedMeshService(weakref.proxy(self))
    self.skeleton = PrecomputedSkeletonService(weakref.proxy(self)) 

  def generate_shared_memory_location(self):
    return 'cloudvolume-shm-' + str(uuid.uuid4())

  def unlink_shared_memory(self):
    return sharedmemory.unlink(self.shared_memory_id)

  @property
  def _storage(self):
    if self.path.protocol == 'boss':
      return None
    
    try:
      return SimpleStorage(self.layer_cloudpath)
    except:
      if self.path.layer == 'info':
        warn("WARNING: Your layer is named 'info', is that what you meant? {}".format(
            self.path
        ))
      raise
      
  @classmethod
  def create_new_info(cls, 
    num_channels, layer_type, data_type, encoding, 
    resolution, voxel_offset, volume_size, 
    mesh=None, skeletons=None, chunk_size=(64,64,64),
    compressed_segmentation_block_size=(8,8,8)
  ):
    """
    Used for creating new neuroglancer info files.

    Required:
      num_channels: (int) 1 for grayscale, 3 for RGB 
      layer_type: (str) typically "image" or "segmentation"
      data_type: (str) e.g. "uint8", "uint16", "uint32", "float32"
      encoding: (str) "raw" for binaries like numpy arrays, "jpeg"
      resolution: int (x,y,z), x,y,z voxel dimensions in nanometers
      voxel_offset: int (x,y,z), beginning of dataset in positive cartesian space
      volume_size: int (x,y,z), extent of dataset in cartesian space from voxel_offset
    
    Optional:
      mesh: (str) name of mesh directory, typically "mesh"
      skeletons: (str) name of skeletons directory, typically "skeletons"
      chunk_size: int (x,y,z), dimensions of each downloadable 3D image chunk in voxels
      compressed_segmentation_block_size: (x,y,z) dimensions of each compressed sub-block
        (only used when encoding is 'compressed_segmentation')

    Returns: dict representing a single mip level that's JSON encodable
    """
    info = {
      "num_channels": int(num_channels),
      "type": layer_type,
      "data_type": data_type,
      "scales": [{
        "encoding": encoding,
        "chunk_sizes": [chunk_size],
        "key": "_".join(map(str, resolution)),
        "resolution": list(map(int, resolution)),
        "voxel_offset": list(map(int, voxel_offset)),
        "size": list(map(int, volume_size)),
      }],
    }

    if encoding == 'compressed_segmentation':
      info['scales'][0]['compressed_segmentation_block_size'] = list(map(int, compressed_segmentation_block_size))

    if mesh:
      info['mesh'] = 'mesh' if not isinstance(mesh, string_types) else mesh

    if skeletons:
      info['skeletons'] = 'skeletons' if not isinstance(skeletons, string_types) else skeletons      

    return info

  def refresh_info(self):
    if self.cache.enabled:
      info = self.cache.get_json('info')
      if info:
        self.info = info
        return self.info

    self.info = self._fetch_info()
    self.cache.maybe_cache_info()
    return self.info

  def _fetch_info(self):
    if self.path.protocol == "boss":
      return self.fetch_boss_info()
    
    with self._storage as stor:
      info = stor.get_json('info')

    if info is None:
      raise exceptions.InfoUnavailableError(
        red('No info file was found: {}'.format(self.info_cloudpath))
      )
    return info

  def refreshInfo(self):
    warn("WARNING: refreshInfo is deprecated. Use refresh_info instead.")
    return self.refresh_info()

  def fetch_boss_info(self):
    experiment = ExperimentResource(
      name=self.path.dataset, 
      collection_name=self.path.bucket
    )
    rmt = BossRemote(boss_credentials)
    experiment = rmt.get_project(experiment)

    coord_frame = CoordinateFrameResource(name=experiment.coord_frame)
    coord_frame = rmt.get_project(coord_frame)

    channel = ChannelResource(self.path.layer, self.path.bucket, self.path.dataset)
    channel = rmt.get_project(channel)    

    unit_factors = {
      'nanometers': 1,
      'micrometers': 1e3,
      'millimeters': 1e6,
      'centimeters': 1e7,
    }

    unit_factor = unit_factors[coord_frame.voxel_unit]

    cf = coord_frame
    resolution = [ cf.x_voxel_size, cf.y_voxel_size, cf.z_voxel_size ]
    resolution = [ int(round(_)) * unit_factor for _ in resolution ]

    bbox = Bbox(
      (cf.x_start, cf.y_start, cf.z_start),
      (cf.x_stop, cf.y_stop, cf.z_stop)
    )
    bbox.maxpt = bbox.maxpt 

    layer_type = 'unknown'
    if 'type' in channel.raw:
      layer_type = channel.raw['type']

    info = CloudVolume.create_new_info(
      num_channels=1,
      layer_type=layer_type,
      data_type=channel.datatype,
      encoding='raw',
      resolution=resolution,
      voxel_offset=bbox.minpt,
      volume_size=bbox.size3(),
    )

    each_factor = Vec(2,2,1)
    if experiment.hierarchy_method == 'isotropic':
      each_factor = Vec(2,2,2)
    
    factor = each_factor.clone()
    for _ in range(1, experiment.num_hierarchy_levels):
      self.add_scale(factor, info=info)
      factor *= each_factor

    return info

  def commitInfo(self):
    warn("WARNING: commitInfo is deprecated use commit_info instead.")
    return self.commit_info()

  def commit_info(self):
    if self.path.protocol == 'boss':
      return self 

    for scale in self.scales:
      if scale['encoding'] == 'compressed_segmentation':
        if 'compressed_segmentation_block_size' not in scale.keys():
          raise KeyError("""
            'compressed_segmentation_block_size' must be set if 
            compressed_segmentation is set as the encoding.

            A typical value for compressed_segmentation_block_size is (8,8,8)

            Info file specification:
            https://github.com/google/neuroglancer/blob/master/src/neuroglancer/datasource/precomputed/README.md#info-json-file-specification
          """)
        elif self.dtype not in ('uint32', 'uint64'):
          raise ValueError("compressed_segmentation can only be used with uint32 and uint64 data types.")
    infojson = jsonify(self.info, 
      sort_keys=True,
      indent=2, 
      separators=(',', ': ')
    )

    with self._storage as stor:
      stor.put_file('info', infojson, 
        content_type='application/json', 
        cache_control='no-cache'
      )
    self.cache.maybe_cache_info()
    return self

  def refresh_provenance(self):
    if self.cache.enabled:
      prov = self.cache.get_json('provenance')
      if prov:
        self.provenance = DataLayerProvenance(**prov)
        return self.provenance

    self.provenance = self._fetch_provenance()
    self.cache.maybe_cache_provenance()
    return self.provenance

  def _cast_provenance(self, prov):
    if isinstance(prov, DataLayerProvenance):
      return prov
    elif isinstance(prov, string_types):
      prov = json.loads(prov)

    provobj = DataLayerProvenance(**prov)
    provobj.sources = provobj.sources or []  
    provobj.owners = provobj.owners or []
    provobj.processing = provobj.processing or []
    provobj.description = provobj.description or ""
    provobj.validate()
    return provobj

  def _fetch_provenance(self):
    if self.path.protocol == 'boss':
      return self.provenance

    with self._storage as stor:
      if stor.exists('provenance'):
        provfile = stor.get_file('provenance')
        provfile = provfile.decode('utf-8')

        try:
          provfile = json5.loads(provfile)
        except ValueError:
          raise ValueError(red("""The provenance file could not be JSON decoded. 
            Please reformat the provenance file before continuing. 
            Contents: {}""".format(provfile)))
      else:
        provfile = {
          "sources": [],
          "owners": [],
          "processing": [],
          "description": "",
        }

    return self._cast_provenance(provfile)

  def commit_provenance(self):
    if self.path.protocol == 'boss':
      return self.provenance

    prov = self.provenance.serialize()

    # hack to pretty print provenance files
    prov = json.loads(prov)
    prov = jsonify(prov, 
      sort_keys=True,
      indent=2, 
      separators=(',', ': ')
    )

    with self._storage as stor:
      stor.put_file('provenance', prov, 
        content_type='application/json',
        cache_control='no-cache',
      )
    self.cache.maybe_cache_provenance()
    return self.provenance

  @property
  def dataset_name(self):
    return self.path.dataset
  
  @property
  def layer(self):
    return self.path.layer

  @property
  def mip(self):
    return self._mip

  @mip.setter
  def mip(self, mip):
    mip = list(mip) if isinstance(mip, collections.Iterable) else int(mip)
    try:
      if isinstance(mip, list):  # mip specified by voxel resolution
        self._mip = next((i for (i,s) in enumerate(self.scales)
                          if s["resolution"] == mip))
      else:  # mip specified by index into downsampling hierarchy
        self._mip = self.available_mips[mip]
    except Exception:
      if isinstance(mip, list):
        opening_text = "Scale <{}>".format(", ".join(map(str, mip)))
      else:
        opening_text = "MIP {}".format(str(mip))
  
      scales = [ ",".join(map(str, scale)) for scale in self.available_resolutions ]
      scales = [ "<{}>".format(scale) for scale in scales ]
      scales = ", ".join(scales)
      msg = "{} not found. {} available: {}".format(
        opening_text, len(self.available_mips), scales
      )
      raise exceptions.ScaleUnavailableError(msg)

  @property
  def scales(self):
    return self.info['scales']

  @scales.setter
  def scales(self, val):
    self.info['scales'] = val

  @property
  def scale(self):
    return self.mip_scale(self.mip)

  @scale.setter
  def scale(self, val):
    self.info['scales'][self.mip] = val

  def mip_scale(self, mip):
    return self.info['scales'][mip]

  @property
  def base_cloudpath(self):
    return self.path.protocol + "://" + os.path.join(self.path.bucket, self.path.intermediate_path, self.dataset_name)

  @property 
  def cloudpath(self):
    return self.layer_cloudpath

  @property
  def layer_cloudpath(self):
    return os.path.join(self.base_cloudpath, self.layer)

  @property
  def info_cloudpath(self):
    return os.path.join(self.layer_cloudpath, 'info')

  @property
  def cache_path(self):
    return self.cache.path

  @property
  def shape(self):
    """Returns Vec(x,y,z,channels) shape of the volume similar to numpy.""" 
    return self.mip_shape(self.mip)

  def mip_shape(self, mip):
    size = self.mip_volume_size(mip)
    if self.isfortran:
      return Vec(size[0], size[1], size[2], self.num_channels)
    else:
      return Vec(self.num_channels, size[0], size[1], size[2])

  @property
  def volume_size(self):
    """Returns Vec(x,y,z) shape of the volume (i.e. shape - channels) similar to numpy.""" 
    return self.mip_volume_size(self.mip)

  def mip_volume_size(self, mip):
    return Vec(*self.info['scales'][mip]['size'])

  @property
  def available_mips(self):
    """Returns a list of mip levels that are defined."""
    return range(len(self.info['scales']))

  @property
  def available_resolutions(self):
    """Returns a list of defined resolutions."""
    return (s["resolution"] for s in self.scales)

  @property
  def layer_type(self):
    """e.g. 'image' or 'segmentation'"""
    return self.info['type']

  @property
  def dtype(self):
    """e.g. 'uint8'"""
    return self.data_type

  @property
  def data_type(self):
    return self.info['data_type']

  @property
  def encoding(self):
    """e.g. 'raw' or 'jpeg'"""
    return self.mip_encoding(self.mip)

  def mip_encoding(self, mip):
    return self.info['scales'][mip]['encoding']

  @property
  def compressed_segmentation_block_size(self):
    return self.mip_compressed_segmentation_block_size(self.mip)

  def mip_compressed_segmentation_block_size(self, mip):
    if 'compressed_segmentation_block_size' in self.info['scales'][mip]:
      return self.info['scales'][mip]['compressed_segmentation_block_size']
    return None

  @property
  def num_channels(self):
    return int(self.info['num_channels'])

  @property
  def voxel_offset(self):
    """Vec(x,y,z) start of the dataset in voxels"""
    return self.mip_voxel_offset(self.mip)

  def mip_voxel_offset(self, mip):
    return Vec(*self.info['scales'][mip]['voxel_offset'])

  @property 
  def resolution(self):
    """Vec(x,y,z) dimensions of each voxel in nanometers"""
    return self.mip_resolution(self.mip)

  def mip_resolution(self, mip):
    return Vec(*self.info['scales'][mip]['resolution'])

  @property
  def downsample_ratio(self):
    """Describes how downsampled the current mip level is as an (x,y,z) factor triple."""
    return self.resolution / self.mip_resolution(0)

  @property
  def chunk_size(self):
    """Underlying chunk size dimensions in voxels. Synonym for underlying."""
    return self.underlying

  def mip_chunk_size(self, mip):
    return self.mip_underlying(mip)

  @property
  def underlying(self):
    """Underlying chunk size dimensions in voxels. Synonym for chunk_size."""
    return self.mip_underlying(self.mip)

  def mip_underlying(self, mip):
    return Vec(*self.info['scales'][mip]['chunk_sizes'][0])

  @property
  def key(self):
    """The subdirectory within the data layer containing the chunks for this mip level"""
    return self.mip_key(self.mip)

  def mip_key(self, mip):
    return self.info['scales'][mip]['key']

  @property
  def bounds(self):
    """Returns a bounding box for the dataset with dimensions in voxels"""
    return self.mip_bounds(self.mip)

  @property
  def order(self):
    if self.isfortran:
      return 'F'
    else:
      return 'C'

  def mip_bounds(self, mip):
    offset = self.mip_voxel_offset(mip)
    shape = self.mip_volume_size(mip)
    return Bbox( offset, offset + shape )

  def bbox_to_mip(self, bbox, mip, to_mip):
    """Convert bbox or slices from one mip level to another."""
    if not isinstance(bbox, Bbox):
      bbox = lib.generate_slices(
        bbox, 
        self.mip_bounds(mip).minpt, 
        self.mip_bounds(mip).maxpt, 
        bounded=False
      )
      if self.isfortran:
        bbox = Bbox.from_slices(bbox)
      else:
        bbox = Bbox.from_slices(bbox, order='zyx')

    def one_level(bbox, mip, to_mip):
      original_dtype = bbox.dtype
      # setting type required for Python2
      downsample_ratio = self.mip_resolution(mip).astype(np.float32) / self.mip_resolution(to_mip).astype(np.float32)
      bbox = bbox.astype(np.float64)
      bbox *= downsample_ratio
      bbox.minpt = np.floor(bbox.minpt)
      bbox.maxpt = np.ceil(bbox.maxpt)
      bbox = bbox.astype(original_dtype)
      return bbox

    delta = 1 if to_mip >= mip else -1
    while (mip != to_mip):
      bbox = one_level(bbox, mip, mip + delta)
      mip += delta

    return bbox

  def slices_to_global_coords(self, slices):
    """
    Used to convert from a higher mip level into mip 0 resolution.
    """
    bbox = self.bbox_to_mip(slices, self.mip, 0)
    return bbox.to_slices()

  def slices_from_global_coords(self, slices):
    """
    Used for converting from mip 0 coordinates to upper mip level
    coordinates. This is mainly useful for debugging since the neuroglancer
    client displays the mip 0 coordinates for your cursor.
    """
    bbox = self.bbox_to_mip(slices, 0, self.mip)
    return bbox.to_slices()

      from cloudvolume import CloudVolume

      vol = CloudVolume('gs://mylab/mouse/image', progress=True)
      image = vol[:,:,:] # Download an image stack as a numpy array
      vol[:,:,:] = image # Upload an image stack from a numpy array
      
      label = 1
      mesh = vol.mesh.get(label) 
      skel = vol.skeletons.get(label)

    Required:
      cloudpath: Path to the dataset layer. This should match storage's supported
        providers.

        e.g. Google: gs://$BUCKET/$DATASET/$LAYER/
             S3    : s3://$BUCKET/$DATASET/$LAYER/
             Lcl FS: file:///tmp/$DATASET/$LAYER/
             Boss  : boss://$COLLECTION/$EXPERIMENT/$CHANNEL
             HTTP/S: http(s)://.../$CHANNEL
             matrix: matrix://$BUCKET/$DATASET/$LAYER/
    Optional:
      agglomerate: (bool, graphene only) sets the default mode for downloading
        images to agglomerated (True) vs watershed (False).
      autocrop: (bool) If the specified retrieval bounding box exceeds the
          volume bounds, process only the area contained inside the volume. 
          This can be useful way to ensure that you are staying inside the 
          bounds when `bounded=False`.
      background_color: (number) Specifies what the "background value" of the
        volume is (traditionally 0). This is mainly for changing the behavior
        of delete_black_uploads.
      bounded: (bool) If a region outside of volume bounds is accessed:
          True: Throw an error
          False: Allow accessing the region. If no files are present, an error 
              will still be thrown. Consider combining this option with 
              `fill_missing=True`. However, this can be dangrous as it allows
              missing files and potentially network errors to be intepreted as 
              zeros.
      cache: (bool or str) Store downs and uploads in a cache on disk
            and preferentially read from it before redownloading.
          - falsey value: no caching will occur.
          - True: cache will be located in a standard location.
          - non-empty string: cache is located at this file path

          After initialization, you can adjust this setting via:
          `cv.cache.enabled = ...` which accepts the same values.

      cdn_cache: (int, bool, or str) Sets Cache-Control HTTP header on uploaded 
        image files. Most cloud providers perform some kind of caching. As of 
        this writing, Google defaults to 3600 seconds. Most of the time you'll 
        want to go with the default. 
        - int: number of seconds for cache to be considered fresh (max-age)
        - bool: True: max-age=3600, False: no-cache
        - str: set the header manually
      compress: (bool, str, None) pick which compression method to use.
          None: (default) gzip for raw arrays and no additional compression
            for compressed_segmentation and fpzip.
          bool: 
            True=gzip, 
            False=no compression, Overrides defaults
          str: 
            'gzip': Extension so that we can add additional methods in the future 
                    like lz4 or zstd. 
            'br': Brotli compression, better compression rate than gzip
            '': no compression (same as False).
      compress_level: (int, None) level for compression. Higher number results
          in better compression but takes longer.
        Defaults to 9 for gzip (ranges from 0 to 9).
        Defaults to 5 for brotli (ranges from 0 to 11).
      compress_cache: (None or bool) If not None, override default compression 
          behavior for the cache.
      delete_black_uploads: (bool) If True, on uploading an entirely black chunk,
          issue a DELETE request instead of a PUT. This can be useful for avoiding storing
          tiny files in the region around an ROI. Some storage systems using erasure coding 
          don't do well with tiny file sizes.
      fill_missing: (bool) If a chunk file is unable to be fetched:
          True: Use a block of zeros
          False: Throw an error
      green_threads: (bool) Use green threads instead of preemptive threads. This
        can result in higher download performance for some compression types. Preemptive
        threads seem to reduce performance on multi-core machines that aren't densely
        loaded as the CPython threads are assigned to multiple cores and the thrashing
        + GIL reduces performance. You'll need to add the following code to the top
        of your program to use green threads:

            import gevent.monkey
            gevent.monkey.patch_all(threads=False)

      info: (dict) In lieu of fetching a neuroglancer info file, use this one.
          This is useful when creating new datasets and for repeatedly initializing
          a new cloudvolume instance.
      max_redirects: (int) if > 0, allow up to this many redirects via info file 'redirect'
          data fields. If <= 0, allow no redirections and access the current info file directly
          without raising an error.
      mesh_dir: (str) if not None, override the info['mesh'] key before pulling the
        mesh info file.
      mip: (int or iterable) Which level of downsampling to read and write from.
          0 is the highest resolution. You can also specify the voxel resolution
          like mip=[6,6,30] which will search for the appropriate mip level.
      non_aligned_writes: (bool) Enable non-aligned writes. Not multiprocessing 
          safe without careful design. When not enabled, a 
          cloudvolume.exceptions.AlignmentError is thrown for non-aligned writes. 
          
          https://github.com/seung-lab/cloud-volume/wiki/Advanced-Topic:-Non-Aligned-Writes

      parallel (int: 1, bool): Number of extra processes to launch, 1 means only 
          use the main process. If parallel is True use the number of CPUs 
          returned by multiprocessing.cpu_count(). When parallel > 1, shared
          memory (Linux) or emulated shared memory via files (other platforms) 
          is used by the underlying download.
      progress: (bool) Show progress bars. 
          Defaults to True in interactive python, False in script execution mode.
      provenance: (string, dict) In lieu of fetching a provenance 
          file, use this one. 
      secrets: (dict) provide per-instance authorization tokens. If not provided,
        defaults to looking in .cloudvolume/secrets for necessary tokens.
      skel_dir: (str) if not None, override the info['skeletons'] key before 
        pulling the skeleton info file.
      use_https: (bool) maps gs:// and s3:// to their respective https paths. The 
        https paths hit a cached, read-only version of the data and may be faster.
    """
    if use_https:
      cloudpath = to_https_protocol(cloudpath)

    kwargs = dict(locals())
    del kwargs['cls']

  def __realized_bbox(self, requested_bbox):
    """
    The requested bbox might not be aligned to the underlying chunk grid 
    or even outside the bounds of the dataset. Convert the request into
    a bbox representing something that can be actually downloaded.

    Returns: Bbox
    """
    realized_bbox = requested_bbox.expand_to_chunk_size(self.underlying, offset=self.voxel_offset)
    return Bbox.clamp(realized_bbox, self.bounds)

  def exists(self, bbox_or_slices):
    """
    Produce a summary of whether all the requested chunks exist.

    bbox_or_slices: accepts either a Bbox or a tuple of slices representing
      the requested volume. 
    Returns: { chunk_file_name: boolean, ... }
    """
    if type(bbox_or_slices) is Bbox:
      requested_bbox = bbox_or_slices
    else:
      (requested_bbox, _, _) = self.__interpret_slices(bbox_or_slices)
    realized_bbox = self.__realized_bbox(requested_bbox)
    cloudpaths = txrx.chunknames(realized_bbox, self.bounds, self.key, self.underlying)
    cloudpaths = list(cloudpaths)

    with Storage(self.layer_cloudpath, progress=self.progress) as storage:
      existence_report = storage.files_exist(cloudpaths)
    return existence_report

  def delete(self, bbox_or_slices):
    """
    Delete the files within the bounding box.

    bbox_or_slices: accepts either a Bbox or a tuple of slices representing
      the requested volume. 
    """
    if type(bbox_or_slices) is Bbox:
      requested_bbox = bbox_or_slices
    else:
      (requested_bbox, _, _) = self.__interpret_slices(bbox_or_slices)
    realized_bbox = self.__realized_bbox(requested_bbox)

    if requested_bbox != realized_bbox:
      raise exceptions.AlignmentError(
        "Unable to delete non-chunk aligned bounding boxes. Requested: {}, Realized: {}".format(
        requested_bbox, realized_bbox
      ))

    cloudpaths = txrx.chunknames(realized_bbox, self.bounds, self.key, self.underlying)
    cloudpaths = list(cloudpaths)

    with Storage(self.layer_cloudpath, progress=self.progress) as storage:
      storage.delete_files(cloudpaths)

    if self.cache.enabled:
      with Storage('file://' + self.cache.path, progress=self.progress) as storage:
        storage.delete_files(cloudpaths)

  def transfer_to(self, cloudpath, bbox, block_size=None, compress=True):
    """
    Transfer files from one storage location to another, bypassing
    volume painting. This enables using a single CloudVolume instance
    to transfer big volumes. In some cases, gsutil or aws s3 cli tools
    may be more appropriate. This method is provided for convenience. It
    may be optimized for better performance over time as demand requires.

    cloudpath (str): path to storage layer
    bbox (Bbox object): ROI to transfer
    block_size (int): number of file chunks to transfer per I/O batch.
    compress (bool): Set to False to upload as uncompressed
    """
    if type(bbox) is Bbox:
      requested_bbox = bbox
    else:
      (requested_bbox, _, _) = self.__interpret_slices(bbox)
    realized_bbox = self.__realized_bbox(requested_bbox)

    if requested_bbox != realized_bbox:
      raise exceptions.AlignmentError(
        "Unable to transfer non-chunk aligned bounding boxes. Requested: {}, Realized: {}".format(
          requested_bbox, realized_bbox
        ))

    default_block_size_MB = 50 # MB
    chunk_MB = self.underlying.rectVolume() * np.dtype(self.dtype).itemsize * self.num_channels
    if self.layer_type == 'image':
      # kind of an average guess for some EM datasets, have seen up to 1.9x and as low as 1.1
      # affinites are also images, but have very different compression ratios. e.g. 3x for kempressed
      chunk_MB /= 1.3 
    else: # segmentation
      chunk_MB /= 100.0 # compression ratios between 80 and 800....
    chunk_MB /= 1024.0 * 1024.0

    if block_size:
      step = block_size
    else:
      step = int(default_block_size_MB // chunk_MB) + 1

    try:
      destvol = CloudVolume(cloudpath, mip=self.mip)
    except exceptions.InfoUnavailableError: 
      destvol = CloudVolume(cloudpath, mip=self.mip, info=self.info, provenance=self.provenance.serialize())
      destvol.commit_info()
      destvol.commit_provenance()
    except exceptions.ScaleUnavailableError:
      destvol = CloudVolume(cloudpath)
      for i in range(len(destvol.scales) + 1, len(self.scales)):
        destvol.scales.append(
          self.scales[i]
        )
      destvol.commit_info()
      destvol.commit_provenance()

    num_blocks = np.ceil(self.bounds.volume() / self.underlying.rectVolume()) / step
    num_blocks = int(np.ceil(num_blocks))

    cloudpaths = txrx.chunknames(realized_bbox, self.bounds, self.key, self.underlying)

    pbar = tqdm(
      desc='Transferring Blocks of {} Chunks'.format(step), 
      unit='blocks', 
      disable=(not self.progress),
      total=num_blocks,
    )

    with pbar:
      with Storage(self.layer_cloudpath) as src_stor:
        with Storage(cloudpath) as dest_stor:
          for _ in range(num_blocks, 0, -1):
            srcpaths = list(itertools.islice(cloudpaths, step))
            files = src_stor.get_files(srcpaths)
            files = [ (f['filename'], f['content']) for f in files ]
            dest_stor.put_files(
              files=files, 
              compress=compress, 
              content_type=txrx.content_type(destvol),
            )
            pbar.update()

  def __getitem__(self, slices):
    if type(slices) == Bbox:
      slices = slices.to_slices()

    (requested_bbox, steps, channel_slice) = self.__interpret_slices(slices)

    if self.autocrop:
      requested_bbox = Bbox.intersection(requested_bbox, self.bounds)
    
    if self.path.protocol != 'boss':
      return txrx.cutout(self, requested_bbox, steps, channel_slice, parallel=self.parallel,
        shared_memory_location=self.shared_memory_id, output_to_shared_memory=self.output_to_shared_memory)

  @classmethod
  def create_new_info(cls, *args, **kwargs):
    from .frontends import CloudVolumePrecomputed
    # For backwards compatibility, but this only 
    # makes sense for Precomputed anyway
    return CloudVolumePrecomputed.create_new_info(*args, **kwargs)

  @classmethod
  def from_numpy(cls, 
    arr, 
    vol_path='file:///tmp/image/' + generate_random_string(),
    resolution=(4,4,40), voxel_offset=(0,0,0), 
    chunk_size=(128,128,64), layer_type=None, max_mip=0,
    encoding='raw', compress=None
  ):
    """
    Create a new dataset from a numpy array.

    max_mip: (int) the maximum mip level id in the info file. 
    Note that currently the numpy array can only sit in mip 0,
    the max_mip was only created in info file.
    the numpy array itself was not downsampled. 
    """
    if self.path.protocol == 'boss':
      raise NotImplementedError('BOSS protocol does not support shared memory download.')

    if type(slices) == Bbox:
      slices = slices.to_slices()
    (requested_bbox, steps, channel_slice) = self.__interpret_slices(slices)

    if self.autocrop:
      requested_bbox = Bbox.intersection(requested_bbox, self.bounds)
    
    location = location or self.shared_memory_id
    return txrx.cutout(self, requested_bbox, steps, channel_slice, parallel=self.parallel, 
      shared_memory_location=location, output_to_shared_memory=True)

  def flush_cache(self, preserve=None):
    """See vol.cache.flush"""
    warn("CloudVolume.flush_cache(...) is deprecated. Please use CloudVolume.cache.flush(...) instead.")
    return self.cache.flush(preserve=preserve)

  def _boss_cutout(self, requested_bbox, steps, channel_slice=slice(None)):
    # boss interface only works with fortran order indexing now.
    assert self.isfortran
    bounds = Bbox.clamp(requested_bbox, self.bounds)
    
    if bounds.subvoxel():
      raise exceptions.EmptyRequestException('Requested less than one pixel of volume. {}'.format(bounds))

    x_rng = [ bounds.minpt.x, bounds.maxpt.x ]
    y_rng = [ bounds.minpt.y, bounds.maxpt.y ]
    z_rng = [ bounds.minpt.z, bounds.maxpt.z ]

    layer_type = 'image' if self.layer_type == 'unknown' else self.layer_type

    chan = ChannelResource(
      collection_name=self.path.bucket, 
      experiment_name=self.path.dataset, 
      name=self.path.layer, # Channel
      type=layer_type, 
      datatype=self.dtype,
    )

    rmt = BossRemote(boss_credentials)
    cutout = rmt.get_cutout(chan, self.mip, x_rng, y_rng, z_rng, no_cache=True)
    cutout = cutout.T
    cutout = cutout.astype(self.dtype)
    cutout = cutout[::steps.x, ::steps.y, ::steps.z]

    if len(cutout.shape) == 3:
      cutout = cutout.reshape(tuple(list(cutout.shape) + [ 1 ]))

    if self.bounded or self.autocrop or bounds == requested_bbox:
      return VolumeCutout.from_volume(self, cutout, bounds)

    # This section below covers the case where the requested volume is bigger
    # than the dataset volume and the bounds guards have been switched 
    # off. This is useful for Marching Cubes where a 1px excess boundary
    # is needed.
    shape = list(requested_bbox.size3()) + [ cutout.shape[3] ]
    renderbuffer = np.zeros(shape=shape, dtype=self.dtype, order='F')
    txrx.shade(renderbuffer, requested_bbox, cutout, bounds)
    return VolumeCutout.from_volume(self, renderbuffer, requested_bbox)

  def __setitem__(self, slices, img):
    if isinstance(slices, Bbox):
        slices = slices.to_slices()

    imgshape = list(img.shape)
    if len(imgshape) == 3:
      if self.isfortran:
        imgshape = imgshape + [ self.num_channels ]
      else:
        imgshape = [self.num_channels] + imgshape

    if self.isfortran:
      maxsize = list(self.bounds.maxpt) + [ self.num_channels ]
      minsize = list(self.bounds.minpt) + [ 0 ]
    else:
      maxsize = [ self.num_channels ] + list(self.bounds.maxpt)
      minsize = [ 0 ] + list(self.bounds.minpt)

    slices = generate_slices(slices, minsize, maxsize, bounded=self.bounded)
    

    if self.isfortran:
      bbox = Bbox.from_slices(slices)
      slice_shape = list(bbox.size3()) + [ slices[3].stop - slices[3].start ]
    else:
      bbox = Bbox.from_slices(slices, order='zyx')
      slice_shape = [slices[-4].stop - slices[-4].start] + list(bbox.size3())

    if not np.array_equal(imgshape, slice_shape):
      raise exceptions.AlignmentError("Illegal slicing, Image shape: {} != {} Slice Shape".format(imgshape, slice_shape))

    if self.autocrop:
      if not self.bounds.contains_bbox(bbox):
        cropped_bbox = Bbox.intersection(bbox, self.bounds)
        dmin = np.absolute(bbox.minpt - cropped_bbox.minpt)
        dmax = dmin + cropped_bbox.size3()
        img = img[ dmin[0]:dmax[0], dmin[1]:dmax[1], dmin[2]:dmax[2] ] 
        bbox = cropped_bbox

    if bbox.subvoxel():
      return

    if arr.ndim == 3:
      num_channels = 1
    elif arr.ndim == 4:
      num_channels = arr.shape[-1]
    else:
      txrx.upload_image(self, img, bbox.minpt, parallel=self.parallel)

  def upload_from_shared_memory(self, location, bbox, cutout_bbox=None):
    """
    Upload from a shared memory array. 

    https://github.com/seung-lab/cloud-volume/wiki/Advanced-Topic:-Shared-Memory

    tip: If you want to use slice notation, np.s_[...] will help in a pinch.

    MEMORY LIFECYCLE WARNING: You are responsible for managing the lifecycle of the 
      shared memory. CloudVolume will merely read from it, it will not unlink the 
      memory automatically. To fully clear the shared memory you must unlink the 
      location and close any mmap file handles. You can use `cloudvolume.sharedmemory.unlink(...)`
      to help you unlink the shared memory file.

    EXPERT MODE WARNING: If you aren't sure you need this function (e.g. to relieve 
      memory pressure or improve performance in some way) you should use the ordinary 
      upload method of vol[:] = img. A typical use case is transferring arrays between 
      different processes without making copies. For reference, this feature was created
      for uploading a 62 GB array that originated in Julia.

    Required:
      location: (str) Shared memory location e.g. 'cloudvolume-shm-RANDOM-STRING'
        This typically corresponds to a file in `/dev/shm` or `/run/shm/`. It can 
        also be a file if you're using that for mmap.
      bbox: (Bbox or list of slices) the bounding box the shared array represents. For instance
        if you have a 1024x1024x128 volume and you're uploading only a 512x512x64 corner
        touching the origin, your Bbox would be `Bbox( (0,0,0), (512,512,64) )`.
    Optional:
      cutout_bbox: (bbox or list of slices) If you only want to upload a section of the
        array, give the bbox in volume coordinates (not image coordinates) that should 
        be cut out. For example, if you only want to upload 256x256x32 of the upper 
        rightmost corner of the above example but the entire 512x512x64 array is stored 
        in memory, you would provide: `Bbox( (256, 256, 32), (512, 512, 64) )`

        By default, just upload the entire image.

    Returns: void
    """
    def tobbox(x):
      if isinstance(x, Bbox):
        return x
      else:
        if self.isfortran: 
          return Bbox.from_slices(x)
        else:
          return Bbox.from_slices(x, order='zyx')
        
    bbox = tobbox(bbox)
    cutout_bbox = tobbox(cutout_bbox) if cutout_bbox else bbox.clone()

    if not bbox.contains_bbox(cutout_bbox):
      raise exceptions.AlignmentError("""
        The provided cutout is not wholly contained in the given array. 
        Bbox:        {}
        Cutout:      {}
      """.format(bbox, cutout_bbox))

    if self.autocrop:
      cutout_bbox = Bbox.intersection(cutout_bbox, self.bounds)

    if cutout_bbox.subvoxel():
      return

    if self.isfortran:
      shape = list(bbox.size3()) + [ self.num_channels ]
    else:
      shape = [self.num_channels] + list(bbox.size3())

    mmap_handle, shared_image = sharedmemory.ndarray(
      location=location, shape=shape, dtype=self.dtype, order=order, readonly=True)

    delta_box = cutout_bbox.clone() - bbox.minpt
    cutout_image = shared_image[ delta_box.to_slices() ]
    
    txrx.upload_image(self, cutout_image, cutout_bbox.minpt, parallel=self.parallel, 
      manual_shared_memory_id=location, manual_shared_memory_bbox=bbox, manual_shared_memory_order=order)
    mmap_handle.close() 

  def upload_boss_image(self, img, offset):
    if self.isfortran:
      shape = Vec(*img.shape[:3])
    else:
      shape = Vec(*img.shape[-3:])
    offset = Vec(*offset)

    bounds = Bbox(offset, shape + offset)

    if bounds.subvoxel():
      raise exceptions.EmptyRequestException('Requested less than one pixel of volume. {}'.format(bounds))

    x_rng = [ bounds.minpt.x, bounds.maxpt.x ]
    y_rng = [ bounds.minpt.y, bounds.maxpt.y ]
    z_rng = [ bounds.minpt.z, bounds.maxpt.z ]

    layer_type = 'image' if self.layer_type == 'unknown' else self.layer_type

    chan = ChannelResource(
      collection_name=self.path.bucket, 
      experiment_name=self.path.dataset, 
      name=self.path.layer, # Channel
      type=layer_type, 
      datatype=self.dtype,
    )

    if img.shape[3] == 1:
      img = img.reshape( img.shape[:3] )

    rmt = BossRemote(boss_credentials)
    img = img.T
    img = img.astype(self.dtype)
    if self.isfortran:
      img = np.asfortranarray(img)

    rmt.create_cutout(chan, self.mip, x_rng, y_rng, z_rng, img)

  def save_mesh(self, *args, **kwargs):
    warn("WARNING: vol.save_mesh is deprecated. Please use vol.mesh.save(...) instead.")
    self.mesh.save(*args, **kwargs)
    


