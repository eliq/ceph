#include "rgw_rados.h"
#include "rgw_rest_conn.h"

#define dout_subsys ceph_subsys_rgw

RGWRegionConnection::RGWRegionConnection(CephContext *_cct, RGWRados *store, RGWRegion& upstream) : cct(_cct)
{
  list<string>::iterator iter;
  int i;
  for (i = 0, iter = upstream.endpoints.begin(); iter != upstream.endpoints.end(); ++iter, ++i) {
    endpoints[i] = *iter;
  }
  key = store->zone.system_key;
  region = store->region.name;
}

int RGWRegionConnection::get_url(string& endpoint)
{
  if (endpoints.empty()) {
    ldout(cct, 0) << "ERROR: endpoints not configured for upstream zone" << dendl;
    return -EIO;
  }

  int i = counter.inc();
  endpoint = endpoints[i % endpoints.size()];

  return 0;
}

int RGWRegionConnection::forward(const string& uid, req_info& info, size_t max_response, bufferlist *inbl, bufferlist *outbl)
{
  string url;
  int ret = get_url(url);
  if (ret < 0)
    return ret;
  list<pair<string, string> > params;
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "uid", uid));
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "region", region));
  RGWRESTSimpleRequest req(cct, url, NULL, &params);
  return req.forward_request(key, info, max_response, inbl, outbl);
}

class StreamObjData : public RGWGetDataCB {
  rgw_obj obj;
public:
    StreamObjData(rgw_obj& _obj) : obj(_obj) {}
};

int RGWRegionConnection::put_obj_init(const string& uid, rgw_obj& obj, uint64_t obj_size,
                                      map<string, bufferlist>& attrs, RGWRESTStreamWriteRequest **req)
{
  string url;
  int ret = get_url(url);
  if (ret < 0)
    return ret;

  list<pair<string, string> > params;
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "uid", uid));
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "region", region));
  *req = new RGWRESTStreamWriteRequest(cct, url, NULL, &params);
  return (*req)->put_obj_init(key, obj, obj_size, attrs);
}

int RGWRegionConnection::complete_request(RGWRESTStreamWriteRequest *req, string& etag, time_t *mtime)
{
  int ret = req->complete(etag, mtime);
  delete req;

  return ret;
}

int RGWRegionConnection::get_obj(const string& uid, rgw_obj& obj, bool prepend_metadata, RGWGetDataCB *cb, RGWRESTStreamReadRequest **req)
{
  string url;
  int ret = get_url(url);
  if (ret < 0)
    return ret;

  list<pair<string, string> > params;
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "uid", uid));
  params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "region", region));
  if (prepend_metadata) {
    params.push_back(make_pair<string, string>(RGW_SYS_PARAM_PREFIX "prepend-metadata", region));
  }
  *req = new RGWRESTStreamReadRequest(cct, url, cb, NULL, &params);
  return (*req)->get_obj(key, obj);
}

int RGWRegionConnection::complete_request(RGWRESTStreamReadRequest *req, string& etag, time_t *mtime,
                                          map<string, string>& attrs)
{
  int ret = req->complete(etag, mtime, attrs);
  delete req;

  return ret;
}

